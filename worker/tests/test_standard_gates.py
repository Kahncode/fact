"""
`Standard` with a person in the loop on a file task: a blocked agent's question opens a gate only `fact gate` answers.

Also the status query and steering. Asking on an issue is in `test_standard_questions.py`.
"""

from datetime import timedelta

import pytest
from fact.core.activity_contract import (
    AGENT_STEP,
    APP_SLUG,
    OPEN_CHANGE_SET,
    RECORD_GATE_ANSWERED,
    RECORD_GATE_OPENED,
    WAIT_FOR_ANSWER,
)
from fact.core.enums import RunOutcome, StepOutcome
from fact.core.gates import STATUS, STEER, GateDecision, WorkflowStatus
from fact.core.types import RunResult
from fact.sdk.human import STEERING_HEADING
from fact.worker.loader import sandbox_runner
from gate_harness import answer, open_gate
from standard_harness import STANDARD, FakeActivities, finished, run_standard, started_standard
from standard_question_fakes import blocked
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowHistory, WorkflowUpdateFailedError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer

QUESTION = "Use the v1 or the v2 widget API?"
DAYS = 3


def _implements(fakes: FakeActivities) -> list:
    return [s for s in fakes.of(AGENT_STEP) if s.prompt_name == "implement"]


async def test_approve_resumes_the_blocked_session_with_the_note() -> None:
    fakes = FakeActivities(implement_script=[blocked(QUESTION)], gate_decisions=None)
    async with started_standard(fakes, "run-question-approve") as (_, handle):
        gate = await open_gate(handle)
        assert (gate.gate_id, gate.kind, gate.stage_id, gate.attempt) == (
            "implement-question-1",
            "needs_human",
            "needs_human",
            1,
        )
        assert gate.details["question"] == QUESTION
        await answer(handle, gate.gate_id, GateDecision.APPROVE, "v2")
        result: RunResult = await handle.result()
        history = await handle.fetch_history()

    assert result.outcome == RunOutcome.MERGED
    first, resumed = _implements(fakes)
    assert (first.resume_session_id, resumed.resume_session_id) == (None, "s-1")
    assert QUESTION in resumed.context
    assert "v2" in resumed.context
    (answered,) = fakes.gates(RECORD_GATE_ANSWERED)
    assert (answered.decision, answered.note, answered.outcome) == ("approve", "v2", StepOutcome.PASSED)
    assert fakes.of(APP_SLUG) == fakes.of(WAIT_FOR_ANSWER) == []  # a file task has no issue to watch
    opened = next(r for r in fakes.of("report_status") if r.event == "gate_opened")
    assert QUESTION in opened.detail
    assert "fact gate run-1 approve|reject" in opened.detail

    # The new shape replays: the gate, its update and its control activities are deterministic.
    replayed = WorkflowHistory.from_json("run-question-approve", history.to_json())
    replayer = Replayer(workflows=[STANDARD], data_converter=pydantic_data_converter, workflow_runner=sandbox_runner())
    await replayer.replay_workflow(replayed)


async def test_reject_ends_the_run_needing_a_human() -> None:
    fakes = FakeActivities(implement_script=[blocked(QUESTION)], gate_decisions=None)
    async with started_standard(fakes, "run-question-reject") as (_, handle):
        gate = await open_gate(handle)
        status = await handle.query(STATUS, result_type=WorkflowStatus)
        assert (status.stage, status.attempt) == ("needs_human", 1)
        assert status.gate == gate
        assert status.outcomes[-1].model_dump() == {"stage_id": "implement", "attempt": 1, "outcome": "needs_human"}

        await answer(handle, gate.gate_id, GateDecision.REJECT, "not worth it")
        result: RunResult = await handle.result()
        status = await handle.query(STATUS, result_type=WorkflowStatus)

    assert result.outcome == RunOutcome.NEEDS_HUMAN
    assert QUESTION in result.summary
    assert result.summary.endswith("not worth it")
    assert len(_implements(fakes)) == 1
    assert finished(fakes) == RunOutcome.NEEDS_HUMAN
    assert fakes.of(OPEN_CHANGE_SET) == []
    assert status.gate is None
    assert status.outcomes[-1].model_dump() == {"stage_id": "needs_human", "attempt": 1, "outcome": "needs_human"}


async def test_blocked_without_a_question_ends_the_run_with_no_gate() -> None:
    fakes = FakeActivities(implement_script=[blocked()])
    result, _ = await run_standard(fakes, "run-question-none")

    assert result.outcome == RunOutcome.NEEDS_HUMAN
    assert fakes.gates(RECORD_GATE_OPENED) == []
    assert len(_implements(fakes)) == 1


async def test_each_question_has_its_own_gate_and_a_closed_gate_cannot_be_answered() -> None:
    fakes = FakeActivities(implement_script=[blocked(QUESTION), blocked("And the colour?")], gate_decisions=None)
    async with started_standard(fakes, "run-question-twice") as (_, handle):
        first = await open_gate(handle)
        with pytest.raises(WorkflowUpdateFailedError) as unknown:
            await answer(handle, "nope", GateDecision.APPROVE)
        assert "nope" in str(unknown.value.cause)

        await answer(handle, first.gate_id, GateDecision.APPROVE, "v2")
        second = await open_gate(handle, after=[first.gate_id])
        assert (second.gate_id, second.attempt) == ("implement-question-2", 2)  # stage `needs_human` again
        with pytest.raises(WorkflowUpdateFailedError) as closed:
            await answer(handle, first.gate_id, GateDecision.REJECT)
        assert first.gate_id in str(closed.value.cause)

        await answer(handle, second.gate_id, GateDecision.APPROVE, "blue")
        result: RunResult = await handle.result()

    assert result.outcome == RunOutcome.MERGED
    assert [s.resume_session_id for s in _implements(fakes)] == [None, "s-1", "s-1"]


async def test_steering_reaches_the_next_agent_session_only() -> None:
    fakes = FakeActivities(implement_script=[blocked(QUESTION)], gate_decisions=None)
    async with started_standard(fakes, "run-gate-steer") as (_, handle):
        gate = await open_gate(handle)
        sessions_before = len(fakes.of(AGENT_STEP))
        await handle.signal(STEER, "  use the widget fixture  ")
        await answer(handle, gate.gate_id, GateDecision.APPROVE, "v2")
        result: RunResult = await handle.result()

    assert result.outcome == RunOutcome.MERGED
    sessions = fakes.of(AGENT_STEP)
    assert all(STEERING_HEADING not in s.context for s in sessions[:sessions_before])
    resumed, *later = sessions[sessions_before:]
    assert resumed.prompt_name == "implement"
    assert "v2" in resumed.context  # its own context first
    assert resumed.context.endswith(f"{STEERING_HEADING}\n\nuse the widget fixture")
    assert later
    assert all(STEERING_HEADING not in s.context for s in later)  # cleared once used


async def test_a_gate_open_for_days_costs_nothing() -> None:
    fakes = FakeActivities(implement_script=[blocked(QUESTION)], gate_decisions=None)
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
