"""
The usage gate: `SchedulerTick` with stub task activities and a real `UsageGate` over mocked usage.

On the time-skipping server, so the tick's `workflow.now()` passes reset times.
"""

import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fact.core.activity_contract import CONTROL_QUEUE
from fact.core.claude_usage import ClaudeUsage, UsageUnavailableError, UsageWindow
from fact.core.scheduler import (
    FIND_READY_TASKS,
    START_SCHEDULED_RUN,
    ProjectLoad,
    ReadyTask,
    ReadyTasks,
    StartScheduledRunInput,
    TickResult,
)
from fact.core.settings import ClaudeSettings
from fact.core.usage_gate import GateState
from fact.worker.activities.scheduler import SchedulerActivities
from fact.worker.scheduler import SchedulerTick
from fact.worker.usage_gate import MIN_WAIT, RESET_MARGIN, UsageGate
from structlog.testing import capture_logs
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

HOUR = timedelta(hours=1)
FAR = datetime(2100, 1, 1, tzinfo=UTC)


def _ready(n_tasks: int = 1, active: int = 0, max_runs: int = 2) -> ReadyTasks:
    tasks = [ReadyTask(project="a", task_key=f"task:M1-0{i}", task_id=f"M1-0{i}") for i in range(1, n_tasks + 1)]
    projects = [ProjectLoad(project="a", active=active, max_runs=5)]
    return ReadyTasks(tasks=tasks, projects=projects, active=active, max_runs=max_runs)


def _five_hour(utilization: float, resets_at: datetime) -> ClaudeUsage:
    return ClaudeUsage(windows=(UsageWindow(name="five_hour", utilization=utilization, resets_at=resets_at),))


class Harness:
    """Stub `find_ready_tasks` and `start_scheduled_run`; a mocked usage endpoint (`answer`, `calls`)."""

    def __init__(self, env: WorkflowEnvironment, ready: ReadyTasks) -> None:
        self.env = env
        self.ready = ready
        self.started: list[str] = []
        self.calls = 0
        self.answer: Callable[[], ClaudeUsage] = ClaudeUsage
        self.reported: list[dict[str, Any]] = []

    def fetch(self) -> ClaudeUsage:
        self.calls += 1
        return self.answer()

    def activities(self, http: httpx.AsyncClient, gate: UsageGate) -> list[Callable[..., Any]]:
        @activity.defn(name=FIND_READY_TASKS)
        async def find_ready_tasks() -> ReadyTasks:
            return self.ready

        @activity.defn(name=START_SCHEDULED_RUN)
        async def start_scheduled_run(spec: StartScheduledRunInput) -> str:
            self.started.append(spec.task_key)
            return f"run-{spec.task_key}"

        acts = SchedulerActivities("http://api", http, gate)
        return [find_ready_tasks, start_scheduled_run, acts.check_claude_usage]

    def api(self, request: httpx.Request) -> httpx.Response:
        assert (request.method, request.url.path) == ("PUT", "/api/v1/scheduler/usage")
        self.reported.append(json.loads(request.content))
        return httpx.Response(204)

    async def now(self) -> datetime:
        return await self.env.get_current_time()

    async def tick(self) -> TickResult:
        return await self.env.client.execute_workflow(
            SchedulerTick.run, id=f"tick-{uuid.uuid4()}", task_queue=CONTROL_QUEUE, result_type=TickResult
        )


async def _run(
    ready: ReadyTasks,
    body: Callable[[Harness], Awaitable[None]],
    claude: ClaudeSettings | None = None,
    environ: dict[str, str] | None = None,
) -> Harness:
    env_cm = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    async with env_cm as env:
        h = Harness(env, ready)
        gate = UsageGate(claude or ClaudeSettings(), fetch=h.fetch, environ=environ or {})
        async with httpx.AsyncClient(transport=httpx.MockTransport(h.api)) as http:
            worker = Worker(
                env.client, task_queue=CONTROL_QUEUE, workflows=[SchedulerTick], activities=h.activities(http, gate)
            )
            # time moves only by `env.sleep`: auto-skipping would jump ahead while a tick waits on its activities
            with env.auto_time_skipping_disabled():
                async with worker:
                    await body(h)
    return h


async def test_full_window_waits_for_its_reset_without_calling() -> None:
    async def body(h: Harness) -> None:
        reset = await h.now() + HOUR
        h.answer = lambda: _five_hour(0.95, reset)
        first = await h.tick()
        assert first.started == []
        assert "five_hour 95% (reserve 10%)" in first.usage_full
        assert h.calls == 1
        await h.env.sleep(timedelta(minutes=30))
        assert (await h.tick()).started == []
        assert h.calls == 1  # resets_at is still ahead: no call
        await h.env.sleep(timedelta(minutes=30) + RESET_MARGIN)
        h.answer = lambda: _five_hour(0.02, reset + 5 * HOUR)
        third = await h.tick()
        assert h.calls == 2  # one call confirms the reset  # noqa: PLR2004
        assert third.started == ["run-task:M1-01"]

    h = await _run(_ready(), body)
    assert [r["state"] for r in h.reported] == ["full", "ok"]  # a cached answer is not reported again
    assert h.reported[0]["windows"][0]["reserve"] == 0.10  # noqa: PLR2004


async def test_window_still_full_past_its_reset_backs_off() -> None:
    async def body(h: Harness) -> None:
        past = await h.now() - RESET_MARGIN - timedelta(minutes=1)
        h.answer = lambda: _five_hour(0.99, past)  # the reset has passed, but it still reads full
        await h.tick()
        assert h.calls == 1
        await h.env.sleep(MIN_WAIT - timedelta(seconds=10))
        await h.tick()
        assert h.calls == 1  # backing off for MIN_WAIT
        await h.env.sleep(timedelta(seconds=20))
        await h.tick()
        assert h.calls == 2  # then for twice as long  # noqa: PLR2004
        await h.env.sleep(MIN_WAIT + timedelta(seconds=30))
        await h.tick()
        assert h.calls == 2  # noqa: PLR2004
        await h.env.sleep(MIN_WAIT)
        assert (await h.tick()).started == []
        assert h.calls == 3  # noqa: PLR2004

    await _run(_ready(), body)


async def _one_tick(h: Harness) -> None:
    assert (await h.tick()).started == []


async def test_no_call_without_a_task_or_a_slot() -> None:
    assert (await _run(_ready(n_tasks=0), _one_tick)).calls == 0
    assert (await _run(_ready(active=2), _one_tick)).calls == 0


async def test_one_call_per_tick_when_usage_is_ok() -> None:
    async def body(h: Harness) -> None:
        reset = await h.now() + HOUR
        h.answer = lambda: _five_hour(0.5, reset)
        await h.tick()
        await h.tick()
        assert h.calls == 2  # noqa: PLR2004
        assert h.started == ["task:M1-01", "task:M1-01"]  # the stub never fills a slot

    await _run(_ready(), body)


async def test_unreadable_usage_logs_an_error_and_starts_within_the_caps() -> None:
    def refuse() -> ClaudeUsage:
        msg = "no Claude Code credentials in ~/.claude"
        raise UsageUnavailableError(msg)

    async def body(h: Harness) -> None:
        h.answer = refuse
        with capture_logs() as logs:
            result = await h.tick()
        assert result.started == ["run-task:M1-01"]  # a waiting (paused) run holds the other slot
        assert any(e["event"] == "claude_usage_unavailable" and e["log_level"] == "error" for e in logs)

    h = await _run(_ready(n_tasks=3, active=1, max_runs=2), body)
    assert h.reported[-1]["state"] == GateState.UNAVAILABLE
    assert "credentials" in h.reported[-1]["error"]


@pytest.mark.parametrize(
    ("claude", "environ"),
    [
        (ClaudeSettings(usage_gate=False), {}),
        (ClaudeSettings(), {"ANTHROPIC_API_KEY": "sk-test"}),  # pragma: allowlist secret
    ],
)
async def test_gate_off(claude: ClaudeSettings, environ: dict[str, str]) -> None:
    async def body(h: Harness) -> None:
        h.answer = lambda: _five_hour(1.0, FAR)
        assert (await h.tick()).started == ["run-task:M1-01"]

    h = await _run(_ready(), body, claude=claude, environ=environ)
    assert h.calls == 0
    assert h.reported[-1]["state"] == GateState.OFF
