"""The review stage of `Standard`: findings go to `address_review`, an invalid answer fails the run."""

import pytest
from fact.core.activity_contract import AGENT_STEP, OPEN_CHANGE_SET
from fact.core.enums import RunOutcome, StepOutcome
from fact.core.types import StepResult
from standard_harness import (
    HIGH,
    LEARNED,
    LOW,
    MET,
    UNMET,
    FakeActivities,
    agent_sessions,
    checks,
    finished,
    run_standard,
)


def _reviewed(outputs: dict, outcome: StepOutcome = StepOutcome.PASSED) -> StepResult:
    return StepResult(outcome=outcome, summary="reviewed", outputs={"status": "done", "summary": "reviewed", **outputs})


@pytest.mark.parametrize(
    ("criteria", "findings"), [([UNMET], [LOW]), ([MET], [HIGH]), ([MET], [{**HIGH, "severity": "medium"}])]
)
async def test_review_findings_are_addressed_then_checked(criteria: list, findings: list) -> None:
    fakes = FakeActivities(review=_reviewed({"criteria": criteria, "findings": findings}))
    result, _ = await run_standard(fakes, "run-review-findings")

    assert result.outcome == RunOutcome.PR_OPENED
    assert agent_sessions(fakes) == [
        ("implement", 1, "implement"),
        ("review", 1, "review"),
        ("address_review", 1, "address_review"),
        ("learn", 1, "learn"),
    ]
    address = fakes.of(AGENT_STEP)[2]
    assert all(f["message"] in address.context for f in findings)
    assert all(c["text"] in address.context for c in criteria if not c["met"])
    assert [(s.attempt, s.check) for s in checks(fakes)] == [(1, "lint"), (2, "test"), (3, "lint"), (4, "test")]
    (pr_spec,) = fakes.of(OPEN_CHANGE_SET)
    assert pr_spec.title == LEARNED["title"]  # neither review nor address_review rewrites the PR text
    assert [c.text for c in pr_spec.criteria] == [c["text"] for c in criteria]


async def test_checks_failing_after_address_review_go_to_fix_sessions() -> None:
    fakes = FakeActivities(review=_reviewed({"criteria": [MET], "findings": [HIGH]}), test_failures_after_address=1)
    result, _ = await run_standard(fakes, "run-address-then-fix")

    assert result.outcome == RunOutcome.PR_OPENED
    assert [p for _, _, p in agent_sessions(fakes)] == ["implement", "review", "address_review", "fix_checks", "learn"]


async def test_blocked_address_review_needs_a_human() -> None:
    fakes = FakeActivities(
        review=_reviewed({"criteria": [UNMET], "findings": []}), address_outcome=StepOutcome.NEEDS_HUMAN
    )
    result, _ = await run_standard(fakes, "run-address-blocked")

    assert result.outcome == RunOutcome.NEEDS_HUMAN
    assert "address_review ended needs_human" in result.summary
    assert fakes.of(OPEN_CHANGE_SET) == []


@pytest.mark.parametrize(
    "review",
    [
        StepResult(outcome=StepOutcome.FAILED_ERROR, summary="the agent ended without a structured answer: bye"),
        _reviewed({"criteria": [MET]}),  # no findings
        _reviewed({"criteria": [{"text": "x", "met": "maybe"}], "findings": []}),
        _reviewed({"criteria": [], "findings": [{**LOW, "severity": "blocker"}]}),
    ],
)
async def test_review_without_a_valid_answer_fails_the_run(review: StepResult) -> None:
    fakes = FakeActivities(review=review)
    result, _ = await run_standard(fakes, "run-review-invalid")

    assert result.outcome == RunOutcome.FAILED
    assert "review ended failed_error" in result.summary
    assert [p for _, _, p in agent_sessions(fakes)] == ["implement", "review"]
    assert fakes.of(OPEN_CHANGE_SET) == []
    assert finished(fakes) == RunOutcome.FAILED
