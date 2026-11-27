"""`Standard` classifies the change set once CI is green, labels its PRs, and again when a base sync moves a head."""

from fact.core.activity_contract import (
    AGENT_STEP,
    CLASSIFY_SIGNALS,
    LABEL_CHANGE_SET,
    MERGE_CHANGE_SET,
    OPEN_CHANGE_SET,
)
from fact.core.classification import ChangedPath, ClassifySignal
from fact.core.enums import RunOutcome, StepOutcome
from fact.core.types import StepResult
from standard_harness import FakeActivities, run_standard
from standard_inputs import MET, REPO

TWICE = 2
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


async def test_a_base_sync_that_moves_the_head_classifies_again() -> None:
    fakes = FakeActivities(merge_script=["out_of_date"], merges=True, sync_script=["merged"])
    result, _ = await run_standard(fakes, "run-classify-resync")

    assert result.outcome == RunOutcome.MERGED
    first, second = fakes.of(OPEN_CHANGE_SET)
    assert first.classification is None
    assert second.classification is not None  # the push keeps the `## Review` until it is classified again
    assert second.classification.route == "auto_merge"
    assert len(fakes.of(CLASSIFY_SIGNALS)) == TWICE
    labels = fakes.of(LABEL_CHANGE_SET)
    assert [[pr.head_sha for pr in label.prs] for label in labels] == [["abc123"], ["fix1"]]
    names = [n for n, _ in fakes.calls]
    relabel = len(names) - 1 - names[::-1].index(LABEL_CHANGE_SET)
    assert names.index(MERGE_CHANGE_SET) < relabel < len(names) - 1 - names[::-1].index(MERGE_CHANGE_SET)


async def test_a_base_sync_that_leaves_the_head_keeps_the_classification() -> None:
    fakes = FakeActivities(merge_script=["out_of_date"], merges=True, fix_ci_commits=False)
    result, _ = await run_standard(fakes, "run-classify-repush")

    assert result.outcome == RunOutcome.MERGED
    assert len(fakes.of(OPEN_CHANGE_SET)) == TWICE
    assert len(fakes.of(CLASSIFY_SIGNALS)) == 1
    assert len(fakes.of(LABEL_CHANGE_SET)) == 1
