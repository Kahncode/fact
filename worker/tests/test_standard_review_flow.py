"""`Standard` waiting for review: an approval merges once CI is green, changes requested get one `review_reply` round."""

from datetime import UTC, datetime

from fact.core.activity_contract import (
    AGENT_STEP,
    CLASSIFY_SIGNALS,
    MERGE_CHANGE_SET,
    NOTIFY,
    OPEN_CHANGE_SET,
    POST_REPLIES,
    REQUEST_REVIEW,
    WAIT_FOR_CI,
    WAIT_FOR_REVIEW,
    Comment,
)
from fact.core.enums import RunOutcome, StepOutcome
from fact.core.types import StepResult
from fact.worker.loader import sandbox_runner
from standard_harness import STANDARD, FakeActivities, agent_sessions, finished, run_standard
from standard_inputs import PR, run_input
from temporalio.client import WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer

REVIEWED = run_input().model_copy(update={"auto_merge": False, "merge_method": "rebase"})
COMMENT = Comment(
    id=55, kind="review_comment", author="octo", body="Rename this", created_at=datetime(2026, 11, 27, tzinfo=UTC)
)
REPLIES = [{"comment_id": 55, "body": "Will do."}]


def _reply(decision: str, instructions: str = "") -> StepResult:
    outputs = {"status": "done", "decision": decision, "replies": REPLIES, "instructions": instructions}
    return StepResult(outcome=StepOutcome.PASSED, summary=decision, outputs=outputs)


def _names(fakes: FakeActivities) -> list[str]:
    return [n for n, _ in fakes.calls]


async def test_an_approval_waits_for_ci_on_its_head_and_merges_with_the_project_method_though_auto_merge_is_off() -> (
    None
):
    fakes = FakeActivities(review_script=["approved"])
    result, _ = await run_standard(fakes, "run-approved", REVIEWED)

    assert (result.outcome, finished(fakes)) == (RunOutcome.MERGED, RunOutcome.MERGED)
    (merged,) = fakes.of(MERGE_CHANGE_SET)
    assert (merged.prs, merged.method) == ([PR], "rebase")
    names = _names(fakes)
    first_wait = names.index(WAIT_FOR_REVIEW)
    assert WAIT_FOR_CI in names[first_wait : names.index(MERGE_CHANGE_SET)]  # CI read again on the approved head


async def test_an_approval_whose_ci_fix_moves_the_head_needs_a_new_one() -> None:
    fakes = FakeActivities(review_script=["approved", "closed"], ci_failures_after_approval=1)
    result, _ = await run_standard(fakes, "run-approved-fixed", REVIEWED)

    assert result.outcome == RunOutcome.CLOSED  # nobody approved the fix, and the PR was closed
    assert not fakes.of(MERGE_CHANGE_SET)
    assert fakes.of(WAIT_FOR_REVIEW)[-1].prs[0].head_sha == "fix1"  # waited again on the fixed head


async def test_an_approved_pr_github_refuses_to_merge_tells_a_person_and_waits() -> None:
    fakes = FakeActivities(review_script=["approved", "closed"], merges=False)
    result, _ = await run_standard(fakes, "run-approved-refused", REVIEWED)

    assert result.outcome == RunOutcome.CLOSED
    keys = [n.dedupe_key for n in fakes.of(NOTIFY)]
    assert "run-1:merge_refused:101" in keys


async def test_changes_requested_get_one_reply_round_then_a_new_review_request() -> None:
    fakes = FakeActivities(review_script=["changes_requested", "merged"], comments=[COMMENT])
    fakes.review_reply = [_reply("reply")]
    result, _ = await run_standard(fakes, "run-reply", REVIEWED)

    assert result.outcome == RunOutcome.MERGED
    replies = [s for s in agent_sessions(fakes) if s[2] == "review_reply"]
    assert replies == [("review_reply", 1, "review_reply")]
    (posted,) = fakes.of(POST_REPLIES)
    assert ([r.model_dump() for r in posted.replies], posted.comments) == (REPLIES, [COMMENT])
    (requested,) = fakes.of(REQUEST_REVIEW)
    assert (requested.pr, requested.reviewers) == (PR, ["octo"])
    assert len(fakes.of(OPEN_CHANGE_SET)) == 1  # `reply` pushes nothing
    first, second = fakes.of(WAIT_FOR_REVIEW)
    assert (first.handled_reviews, second.handled_reviews) == ([], [101])  # the review is never handled again
    assert first.reviews_since is not None  # a reused PR's reviews from before the run are not answered again


async def test_implement_runs_the_chain_again_on_the_same_pr_without_classifying() -> None:
    fakes = FakeActivities(review_script=["changes_requested", "merged"], comments=[COMMENT])
    fakes.review_reply = [_reply("implement", "Rename `widget` to `gadget`.")]
    result, history = await run_standard(fakes, "run-rework", REVIEWED)

    assert result.outcome == RunOutcome.MERGED
    names = _names(fakes)
    after = names[names.index(POST_REPLIES) :]
    assert CLASSIFY_SIGNALS not in after
    assert after.index(OPEN_CHANGE_SET) < after.index(WAIT_FOR_CI) < after.index(REQUEST_REVIEW)
    reply_at = next(i for i, s in enumerate(fakes.of(AGENT_STEP)) if s.prompt_name == "review_reply")
    rework = [s.prompt_name for s in fakes.of(AGENT_STEP)[reply_at + 1 :]]
    assert rework == ["implement", "review", "learn"]
    implement = fakes.of(AGENT_STEP)[reply_at + 1]
    assert implement.context.startswith("## Review round")
    assert "Rename `widget` to `gadget`." in implement.context
    assert "Rename this" in implement.context
    assert len(fakes.of(OPEN_CHANGE_SET)) == 2  # pushed to the same PR  # noqa: PLR2004

    replayed = WorkflowHistory.from_json("run-rework", history.to_json())
    replayer = Replayer(workflows=[STANDARD], data_converter=pydantic_data_converter, workflow_runner=sandbox_runner())
    await replayer.replay_workflow(replayed)


async def test_a_blocked_review_reply_ends_the_run_needs_human_with_its_pr() -> None:
    blocked = StepResult(outcome=StepOutcome.NEEDS_HUMAN, summary="would change a merged repository")
    fakes = FakeActivities(review_script=["changes_requested"])
    fakes.review_reply = [blocked]
    result, _ = await run_standard(fakes, "run-reply-blocked", REVIEWED)

    assert (result.outcome, result.prs) == (RunOutcome.NEEDS_HUMAN, [PR])
    assert not fakes.of(POST_REPLIES)
    assert not fakes.of(REQUEST_REVIEW)
