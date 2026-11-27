"""The `learn` stage of `Standard`: it runs after review, and its answer titles and describes the PR."""

import pytest
from fact.core.activity_contract import OPEN_CHANGE_SET
from fact.core.enums import RunOutcome, StepOutcome
from fact.core.types import StepResult
from standard_harness import FakeActivities, agent_sessions, finished, run_standard
from standard_inputs import HIGH, LEARNED, MET
from temporalio.exceptions import ApplicationError

IMPLEMENT_TITLE = "feat(widget): add the widget"  # what the fake implement session answers


@pytest.mark.parametrize(
    "learn",
    [
        StepResult(outcome=StepOutcome.PASSED, summary="", outputs={"status": "done"}),  # no title, no summary
        StepResult(outcome=StepOutcome.PASSED, summary="", outputs={"title": "  ", "summary": ""}),  # blank
        StepResult(outcome=StepOutcome.NEEDS_HUMAN, summary="blocked", outputs={**LEARNED, "status": "blocked"}),
        StepResult(outcome=StepOutcome.FAILED_ERROR, summary="the agent ended without a structured answer"),
        ApplicationError("harness missing", non_retryable=True),  # the activity itself fails
    ],
)
async def test_without_learn_text_the_pr_falls_back_to_the_implement_session(learn: StepResult | Exception) -> None:
    fakes = FakeActivities(learn=learn)
    result, _ = await run_standard(fakes, "run-learn-fallback")

    assert result.outcome == RunOutcome.MERGED  # a learn session that does not pass costs only the docs
    assert finished(fakes) == RunOutcome.MERGED
    (pr_spec,) = fakes.of(OPEN_CHANGE_SET)
    assert (pr_spec.title, pr_spec.summary) == (IMPLEMENT_TITLE, "implement done")


async def test_fix_and_address_sessions_never_replace_the_implement_text() -> None:
    review = StepResult(
        outcome=StepOutcome.PASSED,
        summary="reviewed",
        outputs={"status": "done", "summary": "reviewed", "criteria": [MET], "findings": [HIGH]},
    )
    learn = StepResult(outcome=StepOutcome.PASSED, summary="", outputs={"status": "done"})
    fakes = FakeActivities(test_failures=1, review=review, learn=learn)
    await run_standard(fakes, "run-learn-fallback-after-fixes")

    assert [p for _, _, p in agent_sessions(fakes)] == ["implement", "fix_checks", "review", "address_review", "learn"]
    (pr_spec,) = fakes.of(OPEN_CHANGE_SET)
    assert (pr_spec.title, pr_spec.summary) == (IMPLEMENT_TITLE, "implement done")


async def test_task_title_is_the_last_fallback() -> None:
    implement = StepResult(outcome=StepOutcome.PASSED, summary="implemented", outputs={"status": "done"})
    learn = StepResult(outcome=StepOutcome.PASSED, summary="", outputs={"status": "done"})
    fakes = FakeActivities(implement_script=[implement], learn=learn)
    await run_standard(fakes, "run-learn-task-title")

    (pr_spec,) = fakes.of(OPEN_CHANGE_SET)
    assert (pr_spec.title, pr_spec.summary) == ("feat: Add a widget", "implemented")
