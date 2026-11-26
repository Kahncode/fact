"""`Standard` with `require_plan_approval`: an `approve_plan` gate before any work."""

from fact.core.activity_contract import (
    AGENT_STEP,
    OPEN_CHANGE_SET,
    PREPARE_WORKSPACE,
    RECORD_GATE_ANSWERED,
    RECORD_GATE_OPENED,
    REPORT_STATUS,
    TEARDOWN_WORKSPACE,
)
from fact.core.enums import RunOutcome, StepOutcome
from fact.core.gates import GateDecision
from fact.core.types import RunInput, RunResult
from gate_harness import answer, open_gate
from standard_harness import FakeActivities, finished, run_standard, started_standard
from standard_inputs import run_input

CRITERIA = ["The widget renders", "The widget is documented"]


def _gated_input() -> RunInput:
    run = run_input()
    task = run.task.model_copy(update={"acceptance_criteria": CRITERIA})
    return run.model_copy(update={"task": task, "require_plan_approval": True})


async def test_no_gate_when_plan_approval_is_off() -> None:
    fakes = FakeActivities()
    result, _ = await run_standard(fakes, "run-plan-off")

    assert result.outcome == RunOutcome.MERGED
    assert all(g.kind != "approve_plan" for g in fakes.gates(RECORD_GATE_OPENED))


async def test_approve_continues_the_run() -> None:
    fakes = FakeActivities(gate_decisions=[GateDecision.APPROVE])
    result, _ = await run_standard(fakes, "run-plan-approve", _gated_input())

    assert result.outcome == RunOutcome.MERGED
    (opened,) = fakes.gates(RECORD_GATE_OPENED)
    assert (opened.gate_id, opened.kind, opened.stage_id, opened.attempt) == (
        "approve_plan",
        "approve_plan",
        "approve_plan",
        1,
    )
    assert opened.details["title"] == "Add a widget"
    assert opened.details["path"] == "tasks/M1/99-widget.md"
    assert opened.details["acceptance_criteria"] == CRITERIA
    assert fakes.calls[0][0] != PREPARE_WORKSPACE  # the gate comes before the workspace
    names = [name for name, _ in fakes.calls]
    assert names.index(RECORD_GATE_ANSWERED) < names.index(PREPARE_WORKSPACE)
    assert len(fakes.of(OPEN_CHANGE_SET)) == 1


async def test_reject_stops_the_run_with_the_note() -> None:
    fakes = FakeActivities(gate_decisions=None)
    async with started_standard(fakes, "run-plan-reject", _gated_input()) as (_, handle):
        gate = await open_gate(handle)
        assert (gate.gate_id, gate.kind) == ("approve_plan", "approve_plan")
        await answer(handle, gate.gate_id, GateDecision.REJECT, "out of scope this quarter")
        result: RunResult = await handle.result()

    assert result.outcome == RunOutcome.STOPPED
    assert result.summary == "plan rejected: out of scope this quarter"
    assert result.prs == []
    (answered,) = fakes.gates(RECORD_GATE_ANSWERED)
    assert (answered.decision, answered.note, answered.outcome) == (
        "reject",
        "out of scope this quarter",
        StepOutcome.NEEDS_HUMAN,
    )
    assert finished(fakes) == RunOutcome.STOPPED
    assert fakes.of(PREPARE_WORKSPACE) == []
    assert fakes.of(AGENT_STEP) == []
    assert fakes.of(TEARDOWN_WORKSPACE) == []


async def test_the_task_hears_of_the_gate_and_the_rejection() -> None:
    fakes = FakeActivities(gate_decisions=[GateDecision.REJECT])
    await run_standard(fakes, "run-plan-report", _gated_input())

    reports = fakes.of(REPORT_STATUS)
    assert [r.event for r in reports] == ["run_started", "gate_opened", "gate_answered", "stopped"]
    assert "Approve the plan for task:M1-99" in reports[1].detail
    assert "fact gate run-1 approve|reject" in reports[1].detail
    assert reports[3].detail == "plan rejected"
