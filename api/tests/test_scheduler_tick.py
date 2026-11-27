"""
`run_tick` in the API: plan order within the caps, refusals skipped, no overlap, the usage gate only when needed.

Temporal is a fake that records each start: the real start is tested in `test_run_start`.
"""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path

import pytest
from fact.api import scheduler
from fact.api.scheduler import run_tick
from fact.api.usage_gate import UsageGate
from fact.core.claude_usage import ClaudeUsage, UsageUnavailableError
from fact.core.scheduler import ReadyTasks, TickResult
from fact.core.settings import CONFIG_VAR, Settings, reset_settings
from fastapi import FastAPI
from httpx import AsyncClient
from scheduler_env import FakeTemporal, FakeUsage, add_run, five_hour, write_config
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs


@pytest.fixture
def cap() -> int:
    return 3  # acme's runner pool max_concurrent_runs


@pytest.fixture
def settings(tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch, cap: int) -> Iterator[Settings]:
    config = write_config(tmp_path, checkout, project_cap=cap)
    monkeypatch.setenv(CONFIG_VAR, str(config))
    reset_settings()
    yield Settings()
    reset_settings()


def _fakes(app: FastAPI, usage: FakeUsage | None = None, *, forbid: bool = False) -> tuple[FakeTemporal, FakeUsage]:
    temporal, usage = FakeTemporal(forbid=forbid), usage or FakeUsage()
    app.state.temporal = temporal
    app.state.scheduler.gate = UsageGate(app.state.settings.claude, fetch=usage, environ={})
    return temporal, usage


async def test_nothing_ready_touches_neither_temporal_nor_the_gate(app: FastAPI, client: AsyncClient) -> None:
    for n, key in enumerate(("task:M1-02", "task:M2-01", "task:M2-02")):
        await add_run(client, n, key)  # each queued: nothing left to offer (M1-03 waits on M1-02)
    _, usage = _fakes(app, forbid=True)
    assert await run_tick(app.state) == TickResult()
    assert usage.calls == 0


@pytest.mark.parametrize(
    ("cap", "started"),
    [
        (3, ["run-task-M2-01-1", "run-task-M2-02-1", "run-task-M1-02-1"]),  # plan order: depth, then id
        (1, ["run-task-M2-01-1"]),  # acme's pool is full
    ],
)
async def test_tick_starts_in_plan_order_within_the_caps(app: FastAPI, client: AsyncClient, started: list[str]) -> None:
    temporal, usage = _fakes(app)
    response = await client.post("/api/v1/scheduler/tick")
    assert response.status_code == HTTPStatus.OK, response.text
    assert TickResult.model_validate(response.json()) == TickResult(started=started)
    assert temporal.started == started
    assert usage.calls == 1
    assert (await run_tick(app.state)).started == []  # every slot is taken, or every task has its run


async def test_tick_skips_a_start_the_api_refuses(
    app: FastAPI, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = scheduler.find_ready

    async def stale(settings: Settings, session: AsyncSession, *, close_parents: bool = False) -> ReadyTasks:
        ready = await real(settings, session, close_parents=close_parents)
        await add_run(client, 1, "task:M2-01")  # `fact run` lands after the tick's read
        return ready

    monkeypatch.setattr(scheduler, "find_ready", stale)
    temporal, _ = _fakes(app)
    result = await run_tick(app.state)
    assert result.failed == ["task:M2-01"]  # 409: it has a run now
    assert result.started == temporal.started == ["run-task-M2-02-1", "run-task-M1-02-1"]


async def test_overlapping_ticks_start_each_task_once(app: FastAPI) -> None:
    temporal, _ = _fakes(app)
    first, second = await asyncio.gather(run_tick(app.state), run_tick(app.state))
    assert second == TickResult(skipped=True)  # it found the first one running
    assert first.started == ["run-task-M2-01-1", "run-task-M2-02-1", "run-task-M1-02-1"]
    await run_tick(app.state)
    assert len(temporal.started) == len(set(temporal.started)) == len(first.started)


async def test_full_window_starts_nothing(app: FastAPI, client: AsyncClient) -> None:
    resets = datetime.now(UTC) + timedelta(hours=1)
    temporal, usage = _fakes(app, FakeUsage(five_hour(0.95, resets)))
    result = await run_tick(app.state)
    assert (result.started, temporal.started) == ([], [])
    assert "five_hour 95% (reserve 10%)" in result.usage_full
    await run_tick(app.state)
    assert usage.calls == 1  # resets_at is ahead: the gate's answer holds without a call
    preview = (await client.get("/api/v1/scheduler/preview")).json()
    assert preview["usage"]["state"] == "full"
    assert preview["tasks"][0]["hold"] == "usage"


async def test_unreadable_usage_logs_an_error_and_starts(app: FastAPI) -> None:
    def refuse() -> ClaudeUsage:
        msg = "no Claude Code credentials in ~/.claude"
        raise UsageUnavailableError(msg)

    _fakes(app)
    app.state.scheduler.gate = UsageGate(app.state.settings.claude, fetch=refuse, environ={})
    with capture_logs() as logs:
        result = await run_tick(app.state)
    assert len(result.started) == 3  # noqa: PLR2004
    assert any(e["event"] == "claude_usage_unavailable" and e["log_level"] == "error" for e in logs)
