"""`fact.sdk.converse.parse_conversation`: a conversation session's answer, or why it gave none usable."""

import pytest
from fact.core.enums import StepOutcome
from fact.core.types import StepResult
from fact.sdk.converse import parse_conversation

DECISIONS = ("implement", "reply")


def _passed(**outputs: object) -> StepResult:
    return StepResult(outcome=StepOutcome.PASSED, outputs={"status": "done", **outputs})


def test_a_valid_answer_is_parsed() -> None:
    step = _passed(decision="implement", replies=[{"comment_id": 5, "body": "ok"}], instructions="rename it")
    result, answer = parse_conversation(step, DECISIONS)
    assert result is step
    assert answer is not None
    assert (answer.decision, answer.replies[0].comment_id, answer.instructions) == ("implement", 5, "rename it")


@pytest.mark.parametrize(
    "step",
    [
        _passed(decision="merge", replies=[]),  # not a decision on offer
        _passed(decision="reply", replies=[{"body": "no id"}]),  # does not parse
    ],
)
def test_an_unusable_answer_fails_the_step(step: StepResult) -> None:
    result, answer = parse_conversation(step, DECISIONS)
    assert (result.outcome, answer) == (StepOutcome.FAILED_ERROR, None)


def test_a_session_that_did_not_pass_is_returned_as_is() -> None:
    blocked = StepResult(outcome=StepOutcome.NEEDS_HUMAN, summary="blocked")
    assert parse_conversation(blocked, DECISIONS) == (blocked, None)
