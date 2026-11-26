"""`HumanSteps` state outside a workflow: steering, the status query and the answer validator."""

from datetime import UTC, datetime

import pytest
from fact.sdk.human import STEER_MAX_CHARS, STEERING_HEADING
from fact.sdk.types import GateAnswer, GateDecision, OpenGate
from fact.sdk.workflow import FactWorkflow

GATE = OpenGate(gate_id="g-1", kind="needs_human", stage_id="needs_human", attempt=1, opened_at=datetime.now(UTC))


def test_steering_is_appended_once_then_cleared() -> None:
    flow = FactWorkflow()
    flow.steer("  first  ")
    flow.steer("   ")  # blank: ignored
    flow.steer("second")

    assert flow.with_steering("fix the tests") == f"fix the tests\n\n{STEERING_HEADING}\n\nfirst\n\nsecond"
    assert flow.with_steering("fix the tests") == "fix the tests"


def test_steering_alone_is_the_whole_context() -> None:
    flow = FactWorkflow()
    flow.steer("use the fixture")
    assert flow.with_steering("") == f"{STEERING_HEADING}\n\nuse the fixture"


def test_steering_is_capped_to_the_newest_text() -> None:
    flow = FactWorkflow()
    for i in range(5):
        flow.steer(f"{i}" * STEER_MAX_CHARS)
    extra = flow.with_steering("").removeprefix(f"{STEERING_HEADING}\n\n")
    assert extra == "4" * STEER_MAX_CHARS


def test_status_says_why_the_run_waits() -> None:
    flow = FactWorkflow()
    flow.note_waiting("usage limit until 2030-01-02 15:01 UTC")
    assert flow.status().waiting == "usage limit until 2030-01-02 15:01 UTC"
    flow.note_waiting(None)
    assert flow.status().waiting is None


def test_status_tracks_the_latest_stage_and_the_open_gate() -> None:
    flow = FactWorkflow()
    empty = {"stage": None, "attempt": None, "gate": None, "waiting": None, "outcomes": []}
    assert flow.status().model_dump() == empty

    flow.next_attempt("checks")
    flow.next_attempt("checks")
    flow._open_gates[GATE.gate_id] = GATE  # noqa: SLF001 - as `gate` leaves it while it waits
    status = flow.status()
    assert (status.stage, status.attempt, status.gate) == ("checks", 2, GATE)


def test_validator_refuses_unknown_and_closed_gates() -> None:
    flow = FactWorkflow()
    flow._open_gates[GATE.gate_id] = GATE  # noqa: SLF001
    answer = GateAnswer(gate_id="g-1", decision=GateDecision.APPROVE)
    with pytest.raises(ValueError, match="unknown gate 'nope'; open gates: g-1"):
        flow._validate_answer(answer.model_copy(update={"gate_id": "nope"}))  # noqa: SLF001

    flow._validate_answer(answer)  # noqa: SLF001
    flow.answer_gate(answer)
    with pytest.raises(ValueError, match="gate 'g-1' is already closed: answered approve"):
        flow._validate_answer(answer)  # noqa: SLF001
