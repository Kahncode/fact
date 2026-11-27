"""What the scheduler may start: ready tasks read from origin and filtered by runs, the preview and the active runs."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fact.api.scheduler import find_ready
from fact.core.project_config import DEFAULT_MAX_RUNS
from fact.core.scheduler import ReadyTasks
from fact.core.settings import CONFIG_VAR, Settings, reset_settings
from fact.core.usage_gate import GateState, GateWindow, UsageCheck
from fastapi import FastAPI
from httpx import AsyncClient
from scheduler_env import add_run, write_config
from task_repo import PROJECT

PREVIEW = "/api/v1/scheduler/preview"
FAILED = {"status": "finished", "outcome": "failed", "ended_at": "2026-11-26T00:00:00Z"}


@pytest.fixture
def settings(tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    monkeypatch.setenv(CONFIG_VAR, str(write_config(tmp_path, checkout)))
    reset_settings()
    yield Settings()
    reset_settings()


async def _ready(app: FastAPI) -> ReadyTasks:
    async with app.state.sessions() as session:
        return await find_ready(app.state.settings, session)


async def test_ready_tasks_with_deps_done_and_never_run(app: FastAPI) -> None:
    got = await _ready(app)
    # M1-01 is done, M1-03 waits on M1-02 (listed, held), M1-04 is only in the working tree.
    assert sorted((t.task_id, t.depth, t.hold) for t in got.tasks) == [
        ("M1-02", 1, None),
        ("M1-03", 2, "dependency"),
        ("M2-01", 0, None),
        ("M2-02", 0, None),
    ]
    assert (got.active, got.max_runs) == (0, 4)
    assert {p.project: (p.active, p.max_runs) for p in got.projects} == {
        PROJECT: (0, 3),
        "remote": (0, DEFAULT_MAX_RUNS),  # a config fact cannot read takes the default
        "broken": (0, DEFAULT_MAX_RUNS),
    }


async def test_which_tasks_with_runs_are_offered_again(app: FastAPI, client: AsyncClient) -> None:
    await add_run(client, 1, "task:M1-02")  # queued: active
    await add_run(client, 2, "task:M2-01", **FAILED)  # failed: the scheduler retries it
    await add_run(client, 3, "task:M2-02", status="finished", outcome="failed")  # its start never reached Temporal
    got = await _ready(app)
    assert [(t.task_id, t.failed_runs, t.hold) for t in got.tasks] == [
        ("M1-03", 0, "dependency"),
        ("M2-01", 1, None),
        ("M2-02", 0, None),
    ]
    assert got.active == 1
    assert next(p.active for p in got.projects if p.project == PROJECT) == 1
    await add_run(client, 4, "task:M2-02", **{**FAILED, "outcome": "needs_human"})  # a person restarts it
    assert "M2-02" not in [t.task_id for t in (await _ready(app)).tasks]


async def test_failure_cap_and_its_reset_by_a_manual_run(app: FastAPI, client: AsyncClient) -> None:
    for n in range(1, 4):
        await add_run(client, n, "task:M2-01", trigger="scheduler", **FAILED)
    m2_01 = next(t for t in (await _ready(app)).tasks if t.task_id == "M2-01")
    assert (m2_01.failed_runs, m2_01.hold) == (3, "failure_cap")
    preview = (await client.get(PREVIEW)).json()
    held = next(p for p in preview["tasks"] if p["task"]["task_id"] == "M2-01")
    assert held["hold"] == "failure_cap"
    assert "3 failed runs in a row" in held["detail"]
    await add_run(client, 4, "task:M2-01", trigger="manual", **FAILED)  # `fact run`, failed again
    m2_01 = next(t for t in (await _ready(app)).tasks if t.task_id == "M2-01")
    assert (m2_01.failed_runs, m2_01.hold) == (1, None)


async def test_preview_order_reasons_and_usage(app: FastAPI, client: AsyncClient) -> None:
    await add_run(client, 1, "task:M2-02", trigger="scheduler", **FAILED)
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
    window = GateWindow(name="five_hour", utilization=0.93, reserve=0.1, resets_at=resets, full=True)
    app.state.scheduler.gate.last = UsageCheck(
        state=GateState.FULL, windows=[window], reason="five_hour 93%", next_call_at=resets
    )  # the gate's last answer, read as it is: the preview never calls the usage endpoint
    preview = (await client.get(PREVIEW)).json()
    assert preview["usage"]["windows"][0]["reserve"] == 0.1  # noqa: PLR2004
    assert [(p["hold"], p["detail"]) for p in preview["tasks"][:3]] == [("usage", "five_hour 93%")] * 3


async def test_active_runs(client: AsyncClient) -> None:
    await add_run(client, 1, "task:M1-02", status="waiting")  # paused on the usage limit: counts
    await add_run(client, 2, "task:M2-01", **FAILED)
    assert (await client.get("/api/v1/scheduler/active")).json() == {"active": 1}
