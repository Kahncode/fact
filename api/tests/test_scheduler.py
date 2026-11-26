"""`/api/v1/scheduler`: ready tasks read from origin and filtered by runs, and the Schedule's on, off and status."""

from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from http import HTTPStatus
from pathlib import Path

import pytest
from fact.api.app import create_app
from fact.core.scheduler import SCHEDULE_ID, SCHEDULER_TICK
from fact.core.settings import CONFIG_VAR, Settings, reset_settings
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from task_repo import PROJECT, PROJECT_YAML, git, task_file, write
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleIntervalSpec,
    ScheduleSpec,
    ScheduleState,
)
from temporalio.testing import WorkflowEnvironment

READY = "/api/v1/scheduler/ready"


@pytest.fixture
def settings(tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    write(checkout, {"tasks/M2/01-next.md": task_file("M2-01", []), "tasks/M2/02-later.md": task_file("M2-02", [])})
    git(checkout, "add", "tasks/M2")  # M1-04 stays in the working tree only
    git(checkout, "commit", "-m", "more tasks")
    git(checkout, "push", "origin", "main")
    broken = tmp_path / "broken"  # a valid config, but not a git checkout: skipped, not fatal
    write(broken, {".fact/project.yaml": PROJECT_YAML})
    config = tmp_path / "fact.yaml"
    config.write_text(
        f"data_dir: {(tmp_path / 'data').as_posix()}\nconcurrency:\n  max_runs: 4\n"
        f"projects:\n  - name: {PROJECT}\n    home: acme/home\n    path: {checkout.as_posix()}\n    max_runs: 3\n"
        f"  - name: remote\n    home: acme/remote\n  - name: broken\n    home: acme/b\n    path: {broken.as_posix()}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(CONFIG_VAR, str(config))
    reset_settings()
    yield Settings()
    reset_settings()


class _Connection:
    address = "test"

    def __init__(self, client: Client) -> None:
        self._client = client

    async def client(self) -> Client:
        return self._client


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _add_run(client: AsyncClient, n: int, task_key: str, **update: str) -> None:
    body = {"workflow_id": f"run-{n}", "project": PROJECT, "task_key": task_key, "workflow_type": "standard"}
    run = (await client.post("/api/v1/runs", json=body)).json()
    if update:
        await client.patch(f"/api/v1/runs/{run['id']}", json=update)


async def test_ready_tasks_with_deps_done_and_never_run(client: AsyncClient) -> None:
    response = await client.get(READY)
    assert response.status_code == HTTPStatus.OK, response.text
    got = response.json()
    # M1-01 is done, M1-03 waits on M1-02, M1-04 is only in the working tree.
    assert sorted((t["task_id"], t["depth"]) for t in got["tasks"]) == [("M1-02", 1), ("M2-01", 0), ("M2-02", 0)]
    assert (got["active"], got["max_runs"]) == (0, 4)
    assert {p["project"]: (p["active"], p["max_runs"]) for p in got["projects"]} == {
        PROJECT: (0, 3),
        "remote": (0, 1),
        "broken": (0, 1),
    }


async def test_a_task_with_a_run_is_not_offered_again(client: AsyncClient) -> None:
    await _add_run(client, 1, "task:M1-02")  # queued: active
    ended = {"status": "finished", "outcome": "failed", "ended_at": "2026-11-26T00:00:00Z"}
    await _add_run(client, 2, "task:M2-01", **ended)  # ended: a person restarts it with `fact run`
    await _add_run(client, 3, "task:M2-02", status="finished", outcome="failed")  # its start never reached Temporal
    got = (await client.get(READY)).json()
    assert [t["task_id"] for t in got["tasks"]] == ["M2-02"]
    assert got["active"] == 1
    assert next(p["active"] for p in got["projects"] if p["project"] == PROJECT) == 1


async def test_scheduler_on_off_status(app: FastAPI, client: AsyncClient) -> None:
    async with await WorkflowEnvironment.start_local() as env:  # the time-skipping server has no Schedules
        app.state.temporal = _Connection(env.client)
        assert (await client.get("/api/v1/scheduler")).status_code == HTTPStatus.NOT_FOUND
        assert (await client.post("/api/v1/scheduler/on")).status_code == HTTPStatus.NOT_FOUND
        schedule = Schedule(
            action=ScheduleActionStartWorkflow(SCHEDULER_TICK, id="tick", task_queue="control"),
            spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(seconds=30))]),
            state=ScheduleState(paused=True),
        )
        await env.client.create_schedule(SCHEDULE_ID, schedule)

        state = (await client.get("/api/v1/scheduler")).json()
        assert (state["schedule_id"], state["paused"], state["interval_seconds"]) == (SCHEDULE_ID, True, 30.0)
        on = await client.post("/api/v1/scheduler/on")
        assert on.status_code == HTTPStatus.OK, on.text
        assert on.json()["paused"] is False
        assert on.json()["next_ticks"]
        off = (await client.post("/api/v1/scheduler/off")).json()
        assert off["paused"] is True
        assert "fact scheduler off" in off["note"]
        assert (await env.client.get_schedule_handle(SCHEDULE_ID).describe()).schedule.state.paused
