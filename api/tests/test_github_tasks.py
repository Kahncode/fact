"""`gh:` task keys through the API: a project whose task source is GitHub Issues, against a fake GitHub."""

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path

import httpx
import pytest
from fact.api.app import create_app
from fact.api.routers import run_start as run_start_router
from fact.api.run_start import find_task, run_input
from fact.api.scheduler import _ready_in
from fact.core.github import GitHubAppConfigError, github_client
from fact.core.github import app as github_app
from fact.core.scheduler import Hold
from fact.core.settings import CONFIG_VAR, Settings, reset_settings
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from task_repo import write

PROJECT = "issues"
PROJECT_YAML = """name: issues
repositories:
  - full_name: acme/app
    role: app
task_source:
  kind: github
  repo: acme/app
workflows:
  default: standard
  bug: bugfix
"""
ISSUES = {
    12: {"number": 12, "title": "Fix it", "body": "Depends on #3\n", "state": "open", "state_reason": None},
    13: {"number": 13, "title": "Then this", "body": "Depends on #12\n", "state": "open", "state_reason": None},
    3: {"number": 3, "title": "Done", "body": "", "state": "closed", "state_reason": "completed"},
    20: {"number": 20, "title": "A plan", "body": "", "state": "open", "state_reason": None},
    21: {"number": 21, "title": "Its step", "body": "", "state": "open", "state_reason": None},
}
for _n, _issue in ISSUES.items():
    _issue["labels"] = [{"name": "fact:ready"}, {"name": "type:bug"}] if _n in {12, 13, 21} else []
ISSUES[20]["labels"] = [{"name": "fact:planned"}]
PARENTS = {21: 20}  # sub-issue -> parent


def _github(request: httpx.Request) -> httpx.Response:
    parts = request.url.path.strip("/").split("/")
    if request.url.path == "/repos/acme/app/issues":
        ready = [i for i in ISSUES.values() if i["state"] == "open" and {"name": "fact:ready"} in i["labels"]]
        return httpx.Response(200, json=ready)
    if parts[-2:] == ["dependencies", "blocked_by"]:
        return httpx.Response(200, json=[])
    if parts[-1] == "sub_issues":
        return httpx.Response(200, json=[ISSUES[c] for c, p in PARENTS.items() if p == int(parts[-2])])
    if parts[-1] == "parent":
        parent = PARENTS.get(int(parts[-2]))
        return (
            httpx.Response(200, json=ISSUES[parent]) if parent else httpx.Response(404, json={"message": "Not Found"})
        )
    issue = ISSUES.get(int(parts[-1])) if parts[1:3] == ["acme", "app"] else None
    return httpx.Response(200, json=issue) if issue else httpx.Response(404, json={"message": "Not Found"})


@dataclass(frozen=True)
class _Token:
    token: str


class _FakeApp:
    async def installation_token(self, full_name: str) -> _Token:
        return _Token(f"tok-{full_name}")

    def api_client(self, token: str | None) -> httpx.AsyncClient:
        return github_client(token, api_url="https://gh.test", transport=httpx.MockTransport(_github))


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    home = tmp_path / "home"  # a GitHub source reads no task files: no git needed
    write(home, {".fact/project.yaml": PROJECT_YAML})
    config = tmp_path / "fact.yaml"
    config.write_text(
        f"data_dir: {(tmp_path / 'data').as_posix()}\n"
        f"projects:\n  - name: {PROJECT}\n    home: acme/app\n    path: {home.as_posix()}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(CONFIG_VAR, str(config))
    monkeypatch.setattr(github_app, "shared_app", lambda _settings=None: _FakeApp())
    reset_settings()
    yield Settings()
    reset_settings()


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    application: FastAPI = create_app(settings)
    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_find_an_issue_and_build_its_run_input(settings: Settings) -> None:
    found = await find_task(settings, "gh:acme/app#12", None)
    assert found.check.missing_deps == []
    assert found.check.task.type == "bug"
    spec = run_input(settings, found, "run-1")
    assert [(r.full_name, r.branch) for r in spec.repos] == [("acme/app", "fact/gh-12")]
    assert spec.task.key == "gh:acme/app#12"


async def test_start_refuses_open_dependencies_and_unknown_issues(client: AsyncClient) -> None:
    response = await client.post("/api/v1/runs/start", json={"task_key": "gh:acme/app#13"})
    assert response.status_code == HTTPStatus.CONFLICT, response.text
    assert response.json()["detail"]["missing_deps"] == ["gh:acme/app#12"]
    response = await client.post("/api/v1/runs/start", json={"task_key": "gh:acme/app#99"})
    assert response.status_code == HTTPStatus.NOT_FOUND


async def test_start_refuses_an_issue_under_a_parent_not_ready(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = await client.post("/api/v1/runs/start", json={"task_key": "gh:acme/app#21"})
    assert response.status_code == HTTPStatus.CONFLICT, response.text
    detail = response.json()["detail"]
    assert detail["not_ready_ancestor"] == "gh:acme/app#20"
    assert "gh:acme/app#20" in detail["message"]

    async def no_temporal(_request: object) -> None:
        raise HTTPException(HTTPStatus.SERVICE_UNAVAILABLE, "no Temporal in this test")

    monkeypatch.setattr(run_start_router, "temporal_client", no_temporal)
    # forced, the gate no longer refuses: the start goes on to Temporal
    body = {"task_key": "gh:acme/app#21", "override_deps": True}
    response = await client.post("/api/v1/runs/start", json=body)
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE, response.text


async def test_start_without_a_github_app_is_a_bad_gateway(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unconfigured(_settings: Settings | None = None) -> None:
        msg = "GitHub App not configured"
        raise GitHubAppConfigError(msg)

    monkeypatch.setattr(github_app, "shared_app", unconfigured)
    body = {"task_key": "gh:acme/app#12", "project": PROJECT}
    response = await client.post("/api/v1/runs/start", json=body)
    assert response.status_code == HTTPStatus.BAD_GATEWAY
    assert "not configured" in response.json()["detail"]


async def test_scheduler_reads_ready_issues(settings: Settings) -> None:
    ready = await _ready_in(settings, settings.projects[0])
    # #13 waits on the open #12: listed, held for the preview
    assert [(t.task_key, t.task_id, t.hold) for t in ready] == [
        ("gh:acme/app#12", "12", None),
        ("gh:acme/app#13", "13", Hold.DEPENDENCY),
        ("gh:acme/app#21", "21", Hold.PARENT),  # its parent #20 is fact:planned
    ]
    assert ready[1].waiting_on == ["gh:acme/app#12"]
    assert ready[2].gated_by == "gh:acme/app#20"
