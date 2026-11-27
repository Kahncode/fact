"""`Standard` when the usage limit refuses an agent session: a durable sleep until the reset, then the same session."""

from datetime import datetime, timedelta

from fact.core.activity_contract import AGENT_STEP, OPEN_CHANGE_SET, RECORD_RUN_WAITING
from fact.core.enums import RunOutcome, StepOutcome
from fact.core.gates import STATUS, WorkflowStatus
from fact.core.types import RunResult, StepResult
from fact.sdk.sessions import LIMIT_PAUSED, MAX_LIMIT_PAUSES
from fact.worker.loader import sandbox_runner
from gate_harness import until_status
from standard_harness import STANDARD, FakeActivities, finished, started_standard
from temporal_env import time_skipping_env
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer

WAIT = timedelta(days=3)  # a full weekly window: a wait of days


async def _now() -> datetime:
    """Read the shared test server's clock: earlier tests have skipped it ahead of the wall clock."""
    return await (await time_skipping_env()).get_current_time()


def _limited(resume_at: datetime, session_id: str | None = "s-1") -> StepResult:
    outputs = {"resume_at": resume_at.isoformat(), "session_id": session_id}
    return StepResult(outcome=StepOutcome.RATE_LIMITED, summary="usage limit", outputs=outputs, session_id=session_id)


RETRYABLE = StepResult(
    outcome=StepOutcome.NEEDS_HUMAN,
    summary="flaky tool",
    outputs={"status": "blocked", "retryable": True, "blocker": "network down"},
    session_id="s-1",
)


async def _waiting(handle: WorkflowHandle) -> WorkflowStatus:
    return await until_status(handle, lambda s: bool(s.waiting))


def _implements(fakes: FakeActivities) -> list:
    return [s for s in fakes.of(AGENT_STEP) if s.prompt_name == "implement"]


async def test_limited_session_sleeps_until_the_reset_and_resumes_it() -> None:
    resume_at = (await _now() + WAIT).replace(microsecond=0)
    fakes = FakeActivities(implement_script=[_limited(resume_at)])
    async with started_standard(fakes, "run-limited") as (env, handle):
        status = await _waiting(handle)
        assert f"{resume_at:%H:%M}" in status.waiting  # says when it resumes
        assert (status.stage, status.attempt) == ("implement", 1)
        result: RunResult = await handle.result()
        now = await env.get_current_time()
        history = await handle.fetch_history()
        after = await handle.query(STATUS, result_type=WorkflowStatus)

    assert result.outcome == RunOutcome.MERGED
    assert now >= resume_at  # the run slept on a durable timer until the reset
    first, resumed = _implements(fakes)
    assert (first.attempt, first.resume_session_id) == (1, None)
    assert (resumed.attempt, resumed.resume_session_id, resumed.context) == (2, "s-1", LIMIT_PAUSED)
    waits = [(w.waiting, w.reason) for w in fakes.of(RECORD_RUN_WAITING)]
    assert waits == [(True, status.waiting), (False, status.waiting)]
    assert after.waiting is None
    assert [o.outcome for o in after.outcomes if o.stage_id == "implement"] == [
        StepOutcome.RATE_LIMITED,
        StepOutcome.PASSED,
    ]
    timers = [e for e in history.events if e.event_type == EventType.EVENT_TYPE_TIMER_STARTED]
    assert any(e.timer_started_event_attributes.start_to_fire_timeout.ToTimedelta() > WAIT / 2 for e in timers)

    replayer = Replayer(workflows=[STANDARD], data_converter=pydantic_data_converter, workflow_runner=sandbox_runner())
    await replayer.replay_workflow(history)


async def test_stop_during_the_sleep_ends_the_run_stopped() -> None:
    fakes = FakeActivities(implement_script=[_limited(await _now() + WAIT)])
    async with started_standard(fakes, "run-limited-stop") as (_, handle):
        await _waiting(handle)
        await handle.cancel(reason="not worth the wait")
        result: RunResult = await handle.result()

    assert result.outcome == RunOutcome.STOPPED
    assert len(_implements(fakes)) == 1  # never resumed
    assert finished(fakes) == RunOutcome.STOPPED


async def test_a_limit_pause_does_not_use_up_the_resumes() -> None:
    resume_at = await _now() + timedelta(hours=5)
    fakes = FakeActivities(implement_script=[_limited(resume_at), RETRYABLE])
    async with started_standard(fakes, "run-limited-then-retryable") as (_, handle):
        result: RunResult = await handle.result()

    assert result.outcome == RunOutcome.MERGED
    assert [(s.attempt, s.resume_session_id) for s in _implements(fakes)] == [(1, None), (2, "s-1"), (3, "s-1")]


async def test_limit_without_a_session_starts_the_session_over() -> None:
    fakes = FakeActivities(implement_script=[_limited(await _now() + timedelta(hours=1), session_id=None)])
    async with started_standard(fakes, "run-limited-no-session") as (_, handle):
        result: RunResult = await handle.result()

    assert result.outcome == RunOutcome.MERGED
    first, again = _implements(fakes)
    assert (again.resume_session_id, again.context) == (None, first.context)


async def test_too_many_limit_pauses_need_a_human() -> None:
    resume_at = await _now() + timedelta(hours=1)  # already past after the first pause: no wait, still counted
    fakes = FakeActivities(implement_script=[_limited(resume_at)] * (MAX_LIMIT_PAUSES + 1))
    async with started_standard(fakes, "run-limited-forever") as (_, handle):
        result: RunResult = await handle.result()

    assert result.outcome == RunOutcome.NEEDS_HUMAN
    assert len(_implements(fakes)) == MAX_LIMIT_PAUSES + 1
    assert fakes.of(OPEN_CHANGE_SET) == []
    assert finished(fakes) == RunOutcome.NEEDS_HUMAN
