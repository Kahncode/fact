"""`gh:` task keys through the API: a project whose task source is GitHub Issues, against a fake GitHub."""

from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path

import httpx
import pytest
from fact.api.routers import run_start as run_start_router
from fact.api.run_start import find_task, run_input
from fact.api.scheduler import _ready_in, run_tick
from fact.api.usage_gate import UsageGate
from fact.core.github import GitHubAppConfigError
from fact.core.github import app as github_app
from fact.core.scheduler import Hold
from fact.core.settings import CONFIG_VAR, Settings, reset_settings
from fastapi import FastAPI, HTTPException
from httpx import AsyncClient
from issues_env import PROJECT, FakeApp, FakeIssues, write_issues_config
from scheduler_env import FakeTemporal, FakeUsage

ENDED = "2026-11-26T00:00:00Z"
HELD, READY, NEEDS_HUMAN = "gh:acme/app#14", "fact:ready", "fact:needs-human"


@pytest.fixture
def github() -> FakeIssues:
    return FakeIssues()


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, github: FakeIssues) -> Iterator[Settings]:
    monkeypatch.setenv(CONFIG_VAR, str(write_issues_config(tmp_path)))
    monkeypatch.setattr(github_app, "shared_app", lambda _settings=None: FakeApp(github))
    reset_settings()
    yield Settings()
    reset_settings()


def _fakes(app: FastAPI, github: FakeIssues) -> FakeTemporal:
    """Temporal records each start in `github.writes` too, so a test sees what came before it."""
    temporal = FakeTemporal()
    real = temporal.start_workflow

    async def start_workflow(wf_type: str, arg: object, *, id: str, task_queue: str) -> None:  # noqa: A002
        github.writes.append(("START", id))
        await real(wf_type, arg, id=id, task_queue=task_queue)

    temporal.start_workflow = start_workflow  # type: ignore[method-assign]
    app.state.temporal = temporal
    app.state.scheduler.gate = UsageGate(app.state.settings.claude, fetch=FakeUsage(), environ={})
    return temporal


async def _add_run(client: AsyncClient, n: int, task_key: str, **update: str) -> None:
    body = {"workflow_id": f"run-{n}", "project": PROJECT, "task_key": task_key, "workflow_type": "standard"}
    run = (await client.post("/api/v1/runs", json={**body, "trigger": "scheduler"})).json()
    await client.patch(f"/api/v1/runs/{run['id']}", json={"status": "finished", "ended_at": ENDED, **update})


async def test_find_an_issue_and_build_its_run_input(settings: Settings) -> None:
    found = await find_task(settings, "gh:acme/app#12", None)
    assert found.check.missing_deps == []
    assert found.check.task.type == "bug"
    spec = run_input(found, "run-1")
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

    async def no_temporal(_connection: object) -> None:
        raise HTTPException(HTTPStatus.SERVICE_UNAVAILABLE, "no Temporal in this test")

    monkeypatch.setattr(run_start_router, "connect_temporal", no_temporal)
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
    ready, _ = await _ready_in(settings, settings.projects[0])
    # #13 waits on the open #12: listed, held for the preview
    assert [(t.task_key, t.task_id, t.hold) for t in ready] == [
        ("gh:acme/app#12", "12", None),
        ("gh:acme/app#13", "13", Hold.DEPENDENCY),
        ("gh:acme/app#14", "14", Hold.NEEDS_HUMAN),  # fact:ready, and fact:needs-human
        ("gh:acme/app#21", "21", Hold.PARENT),  # its parent #20 is fact:planned
    ]
    assert ready[1].waiting_on == ["gh:acme/app#12"]
    assert ready[3].gated_by == "gh:acme/app#20"


async def test_the_label_holds_an_issue_and_run_history_does_not(
    app: FastAPI, client: AsyncClient, github: FakeIssues
) -> None:
    await _add_run(client, 1, "gh:acme/app#12", outcome="stopped")  # stopped, but carries no fact:needs-human
    preview = (await client.get("/api/v1/scheduler/preview")).json()
    holds = {p["task"]["task_key"]: (p["hold"], p["detail"]) for p in preview["tasks"]}
    assert holds["gh:acme/app#12"] == (None, "")
    assert holds[HELD][0] == "needs_human"
    assert f"`fact run {HELD}`" in holds[HELD][1]
    temporal = _fakes(app, github)
    result = await run_tick(app.state)
    assert (len(result.started), result.failed) == (1, [])
    assert temporal.started[0].startswith("run-gh-acme-app-12-")


async def test_the_tick_holds_an_issue_at_the_failure_cap_once(
    app: FastAPI, client: AsyncClient, github: FakeIssues
) -> None:
    for n in range(1, 4):
        await _add_run(client, n, "gh:acme/app#12", outcome="failed")
    temporal = _fakes(app, github)
    await run_tick(app.state)
    assert {READY, NEEDS_HUMAN} <= github.labels(12)
    writes = [w for w in github.writes if w[1].startswith("/repos/acme/app/issues/12/")]
    issue = "/repos/acme/app/issues/12"
    assert [w for w in writes if w[0] == "POST"] == [("POST", f"{issue}/labels"), ("POST", f"{issue}/comments")]
    await run_tick(app.state)
    assert [w for w in github.writes if w[1].startswith("/repos/acme/app/issues/12/")] == writes
    held = [n for n in (await client.get("/api/v1/notifications")).json() if n["kind"] == "task_held"]
    assert [n["title"] for n in held] == ["gh:acme/app#12 is held: its last 3 runs failed in a row"]
    assert temporal.started == []


async def test_the_tick_retries_a_capped_issue_once_a_person_removed_its_label(
    app: FastAPI, client: AsyncClient, github: FakeIssues
) -> None:
    for n in range(1, 4):
        await _add_run(client, n, "gh:acme/app#12", outcome="failed")
    temporal = _fakes(app, github)
    await run_tick(app.state)
    github.issues[12]["labels"] = [{"name": READY}]  # removed by hand
    await run_tick(app.state)
    assert len(temporal.started) == 1


async def test_fact_run_on_a_held_issue_removes_the_label_before_starting(
    app: FastAPI, client: AsyncClient, github: FakeIssues
) -> None:
    _fakes(app, github)
    response = await client.post("/api/v1/runs/start", json={"task_key": HELD})
    assert response.status_code == HTTPStatus.CREATED, response.text
    assert github.writes[-2:] == [
        ("DELETE", "/repos/acme/app/issues/14/labels/fact:needs-human"),
        ("START", response.json()["workflow_id"]),
    ]
    assert NEEDS_HUMAN not in github.labels(14)


async def test_start_refuses_an_issue_whose_ancestor_is_blocked(
    app: FastAPI, client: AsyncClient, github: FakeIssues
) -> None:
    github.issues[30] = {"number": 30, "title": "A ready plan", "body": "", "labels": [{"name": READY}]}
    github.issues[31] = {"number": 31, "title": "Its step", "body": "", "labels": [{"name": READY}]}
    github.__post_init__()
    github.parents[31] = 30
    github.blocked_by[30] = [12]  # #12 is open
    response = await client.post("/api/v1/runs/start", json={"task_key": "gh:acme/app#31"})
    assert response.status_code == HTTPStatus.CONFLICT, response.text
    assert response.json()["detail"]["missing_deps"] == ["gh:acme/app#12"]

    _fakes(app, github)
    body = {"task_key": "gh:acme/app#31", "override_deps": True}
    response = await client.post("/api/v1/runs/start", json=body)
    assert response.status_code == HTTPStatus.CREATED, response.text


async def test_no_run_starts_on_an_issue_closed_since_the_listing(
    app: FastAPI, client: AsyncClient, github: FakeIssues
) -> None:
    listed = github.handle

    def stale(request: httpx.Request) -> httpx.Response:  # the list still shows #12 open, the issue is closed
        response = listed(request)
        if request.url.path == "/repos/acme/app/issues":
            return httpx.Response(200, json=[*response.json(), {**github.issues[12], "state": "open"}])
        return response

    github.handle = stale  # type: ignore[method-assign]
    github.issues[12]["state"] = "closed"
    temporal = _fakes(app, github)
    result = await run_tick(app.state)
    assert (result.started, temporal.started) == ([], [])
    response = await client.post("/api/v1/runs/start", json={"task_key": "gh:acme/app#12"})
    assert response.status_code == HTTPStatus.CONFLICT
    assert "is closed" in response.text


def _listings(github: FakeIssues) -> list[str]:
    """Record the `labels` of each issue list the scheduler asks GitHub for."""
    asked: list[str] = []
    handle = github.handle

    def recorded(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/app/issues":
            asked.append(request.url.params["labels"])
        return handle(request)

    github.handle = recorded  # type: ignore[method-assign]
    return asked


def _new_issue(github: FakeIssues, n: int, *labels: str) -> None:
    names = [READY, "type:bug", *labels]
    github.issues[n] = {
        "number": n,
        "title": "New",
        "body": "",
        "state": "open",
        "labels": [{"name": x} for x in names],
    }


async def test_between_listings_only_a_new_urgent_issue_makes_the_tick_list_again(
    app: FastAPI, github: FakeIssues
) -> None:
    asked, temporal = _listings(github), _fakes(app, github)
    await run_tick(app.state)  # lists, starts #12; the pool (2) has one slot left
    _new_issue(github, 30)
    await run_tick(app.state)
    assert asked == [READY, f"{READY},priority:urgent"]  # #30 waits for the next full listing
    _new_issue(github, 31, "priority:urgent")
    await run_tick(app.state)
    assert asked[2:] == [f"{READY},priority:urgent", READY]
    assert [w.split("-")[4] for w in temporal.started] == ["12", "31"]


async def test_an_empty_listing_is_listed_again_at_every_tick(app: FastAPI, github: FakeIssues) -> None:
    for issue in github.issues.values():
        issue["labels"] = [label for label in issue["labels"] if label["name"] != READY]
    asked, temporal = _listings(github), _fakes(app, github)
    await run_tick(app.state)
    _new_issue(github, 30)
    await run_tick(app.state)
    assert asked == [READY, READY]
    assert len(temporal.started) == 1
