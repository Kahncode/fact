"""`gh:` task keys through the API: a project whose task source is GitHub Issues, against a fake GitHub."""

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path

import httpx
import pytest
from fact.api.app import create_app
from fact.api.run_start import find_task, run_input
from fact.api.scheduler import _ready_in
from fact.core.github import GitHubAppConfigError, github_client
from fact.core.github import app as github_app
from fact.core.settings import CONFIG_VAR, Settings, reset_settings
from fastapi import FastAPI
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
}
for _n, _issue in ISSUES.items():
    _issue["labels"] = [{"name": "fact:ready"}, {"name": "type:bug"}] if _n != 3 else []  # noqa: PLR2004


def _github(request: httpx.Request) -> httpx.Response:
    parts = request.url.path.strip("/").split("/")
    if request.url.path == "/repos/acme/app/issues":
        return httpx.Response(200, json=[i for i in ISSUES.values() if i["state"] == "open"])
    if parts[-2:] == ["dependencies", "blocked_by"]:
        return httpx.Response(200, json=[])
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
    assert [(t.task_key, t.task_id) for t in ready] == [("gh:acme/app#12", "12")]
