"""`Standard` classifies the change set once CI is green, labels its PRs, and later pushes keep the `## Review`."""

from fact.core.activity_contract import AGENT_STEP, CLASSIFY_SIGNALS, LABEL_CHANGE_SET, OPEN_CHANGE_SET
from fact.core.classification import ChangedPath, ClassifySignal
from fact.core.enums import RunOutcome, StepOutcome
from fact.core.types import StepResult
from standard_harness import FakeActivities, run_standard
from standard_inputs import MET, REPO

SCHEMA = ClassifySignal(name="schema_change", paths=[ChangedPath(repo=REPO, path="core/migrations/0009.py")])


async def test_a_signal_routes_to_review_and_the_session_gets_the_context() -> None:
    fakes = FakeActivities(signals=[SCHEMA], ci_failures=1)
    result, _ = await run_standard(fakes, "run-classify-signal")

    assert result.outcome == RunOutcome.MERGED  # merging still ignores the route until auto-merge exists
    assert len(fakes.of(CLASSIFY_SIGNALS)) == 1  # once, after CI is green, not after the failed CI
    session = fakes.of(AGENT_STEP)[-1]
    assert (session.stage_id, session.prompt_name) == ("classify", "classify")
    assert "`schema_change`" in session.context
    assert MET["text"] in session.context  # the review's verdict
    (labelled,) = fakes.of(LABEL_CHANGE_SET)
    classification = labelled.change_set.classification
    assert classification.route == "human_review"
    assert classification.reasons == ["changes the database schema: `core/migrations/0009.py`"]
    assert (classification.risk, classification.door) == ("low", "two_way")


async def test_a_session_without_an_answer_routes_to_review() -> None:
    fakes = FakeActivities(classify=StepResult(outcome=StepOutcome.FAILED_ERROR, summary="no structured answer"))
    result, _ = await run_standard(fakes, "run-classify-failed")

    assert result.outcome == RunOutcome.MERGED
    (labelled,) = fakes.of(LABEL_CHANGE_SET)
    assert labelled.change_set.classification.route == "human_review"
    assert labelled.change_set.classification.risk is None


async def test_a_push_after_classify_keeps_the_review_section() -> None:
    fakes = FakeActivities(merge_script=["out_of_date"], merges=True)
    result, _ = await run_standard(fakes, "run-classify-repush")

    assert result.outcome == RunOutcome.MERGED
    first, second = fakes.of(OPEN_CHANGE_SET)
    assert first.classification is None
    assert second.classification is not None
    assert second.classification.route == "auto_merge"
    assert len(fakes.of(LABEL_CHANGE_SET)) == 1
