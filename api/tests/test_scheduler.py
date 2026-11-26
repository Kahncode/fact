"""`/api/v1/scheduler`: ready tasks read from origin and filtered by runs, and the Schedule's on, off and status."""

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
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
PREVIEW = "/api/v1/scheduler/preview"
FAILED = {"status": "finished", "outcome": "failed", "ended_at": "2026-11-26T00:00:00Z"}


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


async def _add_run(client: AsyncClient, n: int, task_key: str, trigger: str = "manual", **update: str) -> None:
    body = {"workflow_id": f"run-{n}", "project": PROJECT, "task_key": task_key, "workflow_type": "standard"}
    body["trigger"] = trigger
    run = (await client.post("/api/v1/runs", json=body)).json()
    if update:
        await client.patch(f"/api/v1/runs/{run['id']}", json=update)


async def test_ready_tasks_with_deps_done_and_never_run(client: AsyncClient) -> None:
    response = await client.get(READY)
    assert response.status_code == HTTPStatus.OK, response.text
    got = response.json()
    # M1-01 is done, M1-03 waits on M1-02 (listed, held), M1-04 is only in the working tree.
    assert sorted((t["task_id"], t["depth"], t["hold"]) for t in got["tasks"]) == [
        ("M1-02", 1, None),
        ("M1-03", 2, "dependency"),
        ("M2-01", 0, None),
        ("M2-02", 0, None),
    ]
    assert (got["active"], got["max_runs"]) == (0, 4)
    assert {p["project"]: (p["active"], p["max_runs"]) for p in got["projects"]} == {
        PROJECT: (0, 3),
        "remote": (0, 1),
        "broken": (0, 1),
    }


async def test_which_tasks_with_runs_are_offered_again(client: AsyncClient) -> None:
    await _add_run(client, 1, "task:M1-02")  # queued: active
    await _add_run(client, 2, "task:M2-01", **FAILED)  # failed: the scheduler retries it
    await _add_run(client, 3, "task:M2-02", status="finished", outcome="failed")  # its start never reached Temporal
    got = (await client.get(READY)).json()
    assert [(t["task_id"], t["failed_runs"], t["hold"]) for t in got["tasks"]] == [
        ("M1-03", 0, "dependency"),
        ("M2-01", 1, None),
        ("M2-02", 0, None),
    ]
    assert got["active"] == 1
    assert next(p["active"] for p in got["projects"] if p["project"] == PROJECT) == 1
    await _add_run(client, 4, "task:M2-02", **{**FAILED, "outcome": "needs_human"})  # a person restarts it
    assert "M2-02" not in [t["task_id"] for t in (await client.get(READY)).json()["tasks"]]


async def test_failure_cap_and_its_reset_by_a_manual_run(client: AsyncClient) -> None:
    for n in range(1, 4):
        await _add_run(client, n, "task:M2-01", trigger="scheduler", **FAILED)
    m2_01 = next(t for t in (await client.get(READY)).json()["tasks"] if t["task_id"] == "M2-01")
    assert (m2_01["failed_runs"], m2_01["hold"]) == (3, "failure_cap")
    preview = (await client.get(PREVIEW)).json()
    held = next(p for p in preview["tasks"] if p["task"]["task_id"] == "M2-01")
    assert held["hold"] == "failure_cap"
    assert "3 failed runs in a row" in held["detail"]
    await _add_run(client, 4, "task:M2-01", trigger="manual", **FAILED)  # `fact run`, failed again
    m2_01 = next(t for t in (await client.get(READY)).json()["tasks"] if t["task_id"] == "M2-01")
    assert (m2_01["failed_runs"], m2_01["hold"]) == (1, None)


async def test_preview_order_reasons_and_usage(client: AsyncClient) -> None:
    await _add_run(client, 1, "task:M2-02", trigger="scheduler", **FAILED)
    preview = (await client.get(PREVIEW)).json()
    assert preview["usage"] is None
    # M2-01 and M2-02 (depth 0) before M1-02 (depth 1); M1-03 waits on M1-02; the cap of 3 for acme holds nothing
    assert [(p["task"]["task_id"], p["hold"]) for p in preview["tasks"]] == [
        ("M2-01", None),
        ("M2-02", None),
        ("M1-02", None),
        ("M1-03", "dependency"),
    ]
    resets = datetime.now(UTC) + timedelta(hours=2)
    window = {"name": "five_hour", "utilization": 0.93, "reserve": 0.1, "resets_at": resets.isoformat(), "full": True}
    check = {"state": "full", "windows": [window], "reason": "five_hour 93%", "next_call_at": resets.isoformat()}
    assert (await client.put("/api/v1/scheduler/usage", json=check)).status_code == HTTPStatus.NO_CONTENT
    preview = (await client.get(PREVIEW)).json()
    assert preview["usage"]["windows"][0]["reserve"] == 0.1  # noqa: PLR2004
    assert preview["usage"]["windows"][0]["resets_at"].startswith(resets.isoformat()[:16])
    assert [(p["hold"], p["detail"]) for p in preview["tasks"][:3]] == [("usage", "five_hour 93%")] * 3


async def test_active_runs(client: AsyncClient) -> None:
    await _add_run(client, 1, "task:M1-02", status="waiting")  # paused on the usage limit: counts
    await _add_run(client, 2, "task:M2-01", **FAILED)
    assert (await client.get("/api/v1/scheduler/active")).json() == {"active": 1}


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
