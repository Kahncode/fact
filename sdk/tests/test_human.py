"""`HumanSteps` state outside a workflow: steering and the status query."""

from fact.sdk.human import STEER_MAX_CHARS, STEERING_HEADING
from fact.sdk.workflow import FactWorkflow


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
    status = flow.status()
    assert (status.stage, status.attempt, status.gate) == ("checks", 2, None)
