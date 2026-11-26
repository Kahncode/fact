"""`Standard` with a person in the loop: `needs_human` gates on exhausted check loops, the status query, steering."""

from datetime import timedelta

import pytest
from fact.core.activity_contract import AGENT_STEP, OPEN_CHANGE_SET, RECORD_GATE_ANSWERED, RECORD_GATE_OPENED
from fact.core.enums import RunOutcome, StepOutcome
from fact.core.gates import STATUS, STEER, GateDecision, WorkflowStatus
from fact.core.types import RunResult
from fact.sdk.human import STEERING_HEADING
from fact.worker.loader import sandbox_runner
from gate_harness import answer, open_gate
from standard_harness import (
    MAX_FIX_ATTEMPTS,
    STANDARD,
    FakeActivities,
    checks,
    finished,
    run_standard,
    started_standard,
)
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowHistory, WorkflowUpdateFailedError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer

ROUNDS = MAX_FIX_ATTEMPTS + 1  # check rounds in one `until_green`
DAYS = 3


async def test_approve_runs_the_checks_loop_once_more() -> None:
    fakes = FakeActivities(test_failures=ROUNDS, gate_decisions=[GateDecision.APPROVE])
    result, history = await run_standard(fakes, "run-gate-approve")

    assert result.outcome == RunOutcome.MERGED
    (opened,) = fakes.gates(RECORD_GATE_OPENED)
    assert (opened.gate_id, opened.kind, opened.stage_id, opened.attempt) == (
        "checks-1",
        "needs_human",
        "needs_human",
        1,
    )
    assert opened.details["loop"] == "checks"
    assert "test" in opened.details["summary"]
    (answered,) = fakes.gates(RECORD_GATE_ANSWERED)
    assert (answered.step_id, answered.decision, answered.outcome) == ("step-checks-1", "approve", StepOutcome.PASSED)
    assert answered.wait_seconds >= 0
    assert len(checks(fakes)) == 2 * (ROUNDS + 1)  # one more round, green
    assert len(fakes.of(OPEN_CHANGE_SET)) == 1

    # The new shape replays: the gate, its update and its two control activities are deterministic.
    replayed = WorkflowHistory.from_json("run-gate-approve", history.to_json())
    replayer = Replayer(workflows=[STANDARD], data_converter=pydantic_data_converter, workflow_runner=sandbox_runner())
    await replayer.replay_workflow(replayed)


async def test_reject_ends_the_run_needing_a_human() -> None:
    fakes = FakeActivities(test_failures=100, gate_decisions=None)
    async with started_standard(fakes, "run-gate-reject") as (_, handle):
        gate = await open_gate(handle)
        status = await handle.query(STATUS, result_type=WorkflowStatus)
        assert (status.stage, status.attempt) == ("needs_human", 1)
        assert status.gate == gate
        assert (gate.gate_id, gate.kind) == ("checks-1", "needs_human")
        assert status.outcomes[0].stage_id == "setup"
        assert status.outcomes[-1].outcome == StepOutcome.FAILED_CHECKS
        assert len(status.outcomes) == 1 + 1 + 2 * ROUNDS + MAX_FIX_ATTEMPTS  # setup, implement, checks, fixes

        await answer(handle, gate.gate_id, GateDecision.REJECT, "not worth it")
        result: RunResult = await handle.result()
        status = await handle.query(STATUS, result_type=WorkflowStatus)

    assert result.outcome == RunOutcome.NEEDS_HUMAN
    assert result.summary.endswith("rejected: not worth it")
    assert result.prs == []
    (answered,) = fakes.gates(RECORD_GATE_ANSWERED)
    assert (answered.decision, answered.note, answered.outcome) == ("reject", "not worth it", StepOutcome.NEEDS_HUMAN)
    assert finished(fakes) == RunOutcome.NEEDS_HUMAN
    assert fakes.of(OPEN_CHANGE_SET) == []
    assert status.gate is None
    assert status.outcomes[-1].model_dump() == {"stage_id": "needs_human", "attempt": 1, "outcome": "needs_human"}


async def test_answering_an_unknown_or_closed_gate_is_an_error() -> None:
    fakes = FakeActivities(test_failures=100, gate_decisions=None)
    async with started_standard(fakes, "run-gate-invalid") as (_, handle):
        first = await open_gate(handle)
        with pytest.raises(WorkflowUpdateFailedError) as unknown:
            await answer(handle, "nope", GateDecision.APPROVE)
        assert "unknown gate 'nope'" in str(unknown.value.cause)

        await answer(handle, first.gate_id, GateDecision.APPROVE)
        second = await open_gate(handle, after=[first.gate_id])
        assert (second.gate_id, second.attempt) == ("checks-2", 2)  # the next attempt of stage `needs_human`
        with pytest.raises(WorkflowUpdateFailedError) as closed:
            await answer(handle, first.gate_id, GateDecision.REJECT)
        assert "gate 'checks-1' is already closed" in str(closed.value.cause)

        await answer(handle, second.gate_id, GateDecision.REJECT)
        result: RunResult = await handle.result()

    assert result.outcome == RunOutcome.NEEDS_HUMAN
    assert [a.gate_id for a in fakes.gates(RECORD_GATE_ANSWERED)] == ["checks-1", "checks-2"]


async def test_steering_reaches_the_next_agent_session_only() -> None:
    fakes = FakeActivities(test_failures=ROUNDS + 1, gate_decisions=None)  # the round after the gate fails once more
    async with started_standard(fakes, "run-gate-steer") as (_, handle):
        gate = await open_gate(handle)
        sessions_before = len(fakes.of(AGENT_STEP))
        await handle.signal(STEER, "  use the widget fixture  ")
        await answer(handle, gate.gate_id, GateDecision.APPROVE)
        result: RunResult = await handle.result()

    assert result.outcome == RunOutcome.MERGED
    sessions = fakes.of(AGENT_STEP)
    assert all(STEERING_HEADING not in s.context for s in sessions[:sessions_before])
    fix, *later = sessions[sessions_before:]
    assert fix.prompt_name == "fix_checks"
    assert "FAILED test_widget" in fix.context  # its own context first
    assert fix.context.endswith(f"{STEERING_HEADING}\n\nuse the widget fixture")
    assert later
    assert all(STEERING_HEADING not in s.context for s in later)  # cleared once used


async def test_a_gate_open_for_days_costs_nothing() -> None:
    fakes = FakeActivities(test_failures=100, gate_decisions=None)
    async with started_standard(fakes, "run-gate-days") as (env, handle):
        gate = await open_gate(handle)
        calls = len(fakes.calls)
        await env.sleep(timedelta(days=DAYS))
        assert len(fakes.calls) == calls  # no activity, no polling while it waits
        await answer(handle, gate.gate_id, GateDecision.REJECT)
        await handle.result()
        history = await handle.fetch_history()

    (answered,) = fakes.gates(RECORD_GATE_ANSWERED)
    assert answered.wait_seconds >= timedelta(days=DAYS).total_seconds()
    assert answered.answered_at - answered.opened_at == timedelta(seconds=answered.wait_seconds)
    assert not [e for e in history.events if e.event_type == EventType.EVENT_TYPE_TIMER_STARTED]  # a wait, no timer
