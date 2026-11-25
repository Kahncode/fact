import pytest
from fact.sdk.review import Review, parse_review, review_context
from fact.sdk.types import StepOutcome, StepResult

MET = {"text": "Tests pass", "met": True, "evidence": "312 passed"}
UNMET = {"text": "Docs updated", "met": False, "evidence": "README untouched"}


def _step(outcome: StepOutcome = StepOutcome.PASSED, **outputs: object) -> StepResult:
    return StepResult(outcome=outcome, summary="reviewed", outputs={"status": "done", **outputs})


def _finding(severity: str, line: int | None = 3) -> dict:
    return {"severity": severity, "file": "a.py", "line": line, "message": f"{severity} issue"}


def test_clean_review_needs_no_changes() -> None:
    step, review = parse_review(_step(criteria=[MET], findings=[_finding("low")]))
    assert step.outcome == StepOutcome.PASSED
    assert review is not None
    assert not review.needs_changes
    assert review.criteria[0].evidence == "312 passed"


@pytest.mark.parametrize(
    ("criteria", "findings"), [([UNMET], []), ([MET], [_finding("medium")]), ([MET], [_finding("high")])]
)
def test_unmet_or_serious_findings_need_changes(criteria: list, findings: list) -> None:
    _, review = parse_review(_step(criteria=criteria, findings=findings))
    assert review is not None
    assert review.needs_changes


@pytest.mark.parametrize(
    "outputs",
    [
        {},
        {"criteria": [MET]},
        {"criteria": "all good", "findings": []},
        {"criteria": [{"text": "x"}], "findings": []},
        {"criteria": [], "findings": [_finding("critical")]},
        {"criteria": [], "findings": [{"severity": "low"}]},
    ],
)
def test_invalid_answer_is_failed_error(outputs: dict) -> None:
    step, review = parse_review(_step(**outputs))
    assert review is None
    assert step.outcome == StepOutcome.FAILED_ERROR
    assert "without a valid structured answer" in step.summary


def test_step_that_did_not_pass_is_returned_as_is() -> None:
    blocked = _step(StepOutcome.NEEDS_HUMAN)
    assert parse_review(blocked) == (blocked, None)


def test_context_lists_unmet_criteria_then_findings_by_severity() -> None:
    review = Review.model_validate(
        {"criteria": [MET, UNMET], "findings": [_finding("low"), _finding("high", None), _finding("medium")]}
    )
    text = review_context(review)
    assert "Docs updated" in text
    assert "README untouched" in text
    assert "Tests pass" not in text
    assert text.index("high issue") < text.index("medium issue") < text.index("low issue")
    assert "`a.py` high issue" in text
    assert "`a.py:3` medium issue" in text
