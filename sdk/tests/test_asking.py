"""`fact.sdk.asking.question_of`: only a blocked answer with a question asks a person."""

import pytest
from fact.core.enums import StepOutcome
from fact.core.types import StepResult
from fact.sdk.asking import question_of


@pytest.mark.parametrize(
    ("outcome", "outputs", "question"),
    [
        (StepOutcome.NEEDS_HUMAN, {"question": "  v1 or v2?  "}, "v1 or v2?"),
        (StepOutcome.NEEDS_HUMAN, {"question": "   "}, None),
        (StepOutcome.NEEDS_HUMAN, {"blocker": "no key"}, None),
        (StepOutcome.PASSED, {"question": "v1 or v2?"}, None),  # done: nothing to ask
    ],
)
def test_question_of(outcome: StepOutcome, outputs: dict, question: str | None) -> None:
    assert question_of(StepResult(outcome=outcome, outputs=outputs)) == question
