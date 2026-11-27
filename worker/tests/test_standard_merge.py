"""
`Standard` once CI is green: merge an `auto_merge` change set itself; else relabel it, tell a person, wait for the PRs.
"""

import pytest
from fact.core.activity_contract import (
    LABEL_CHANGE_SET,
    MERGE_CHANGE_SET,
    NOTIFY,
    POST_COST_COMMENT,
    RECORD_GATE_ANSWERED,
    RECORD_GATE_OPENED,
    RECORD_RUN_FINISHED,
    TEARDOWN_WORKSPACE,
    WAIT_FOR_PR_CLOSED,
    GateAnsweredInput,
    GateOpenedInput,
    NotifyInput,
)
from fact.core.classification import ChangedPath, ClassifySignal
from fact.core.enums import NotificationKind, RunOutcome, StepOutcome
from fact.core.gates import GateDecision
from fact.sdk.pull_requests import REVIEW_STAGE
from standard_env import load_standard
from standard_harness import FakeActivities, finished, run_standard
from standard_inputs import PR, REPO, run_input
from temporalio.client import WorkflowFailureError

MERGE_REFUSED = load_standard().MERGE_REFUSED
SCHEMA = ClassifySignal(name="schema_change", paths=[ChangedPath(repo=REPO, path="core/migrations/0009.py")])

ENDS = [
    ("merged", RunOutcome.MERGED, GateDecision.APPROVE, StepOutcome.PASSED),
    ("closed", RunOutcome.CLOSED, GateDecision.REJECT, StepOutcome.NEEDS_HUMAN),
]


@pytest.mark.parametrize(("state", "outcome", "decision", "step_outcome"), ENDS)
async def test_run_ends_as_its_pr_did_and_tears_down(
    state: str, outcome: RunOutcome, decision: GateDecision, step_outcome: StepOutcome
) -> None:
    fakes = FakeActivities(pr_state=state, merges=False)  # type: ignore[arg-type]
    result, _ = await run_standard(fakes, f"run-pr-{state}")

    assert (result.outcome, result.prs, result.summary) == (outcome, [PR], PR.url)
    assert finished(fakes) == outcome
    assert len(fakes.of(TEARDOWN_WORKSPACE)) == 1
    (waited,) = fakes.of(WAIT_FOR_PR_CLOSED)
    assert waited.prs == [PR]

    opened: GateOpenedInput = fakes.of(RECORD_GATE_OPENED)[-1]
    answered: GateAnsweredInput = fakes.of(RECORD_GATE_ANSWERED)[-1]
    assert (opened.stage_id, opened.kind, opened.attempt) == (REVIEW_STAGE, REVIEW_STAGE, 1)
    assert opened.details["prs"] == [PR.url]
    assert (answered.step_id, answered.decision, answered.outcome) == (
        f"step-{opened.gate_id}",
        decision,
        step_outcome,
    )
    assert answered.note == f"{PR.url}: {state}"
    assert answered.wait_seconds >= 0


async def test_prs_ready_comes_before_the_wait_and_the_cost_comment_once_at_the_end() -> None:
    fakes = FakeActivities(merges=False)
    await run_standard(fakes, "run-pr-order")

    names = [n for n, _ in fakes.calls]
    wait = names.index(WAIT_FOR_PR_CLOSED)
    ready = next(
        i for i, (n, spec) in enumerate(fakes.calls) if n == NOTIFY and spec.kind == NotificationKind.PRS_READY
    )
    assert names.index(RECORD_GATE_OPENED) < wait
    assert ready < wait
    assert names.count(POST_COST_COMMENT) == 1
    assert names.index(POST_COST_COMMENT) > names.index(RECORD_RUN_FINISHED)  # covers every step


async def test_a_failed_wait_ends_its_step_and_fails_the_run() -> None:
    fakes = FakeActivities(broken=WAIT_FOR_PR_CLOSED, merges=False)
    with pytest.raises(WorkflowFailureError):
        await run_standard(fakes, "run-pr-wait-broken")

    answered: GateAnsweredInput = fakes.of(RECORD_GATE_ANSWERED)[-1]
    assert (answered.stage_id, answered.decision, answered.outcome) == (
        REVIEW_STAGE,
        GateDecision.REJECT,
        StepOutcome.NEEDS_HUMAN,
    )
    assert "wait_for_pr_closed is broken" in answered.note
    assert finished(fakes) == RunOutcome.FAILED
    assert fakes.of(TEARDOWN_WORKSPACE) == []  # the workspace stays for a person
    (comment,) = fakes.of(POST_COST_COMMENT)  # a failed run with a PR still gets its cost, after the end is recorded
    assert comment.prs == [PR]
    names = [n for n, _ in fakes.calls]
    assert names.index(POST_COST_COMMENT) > names.index(RECORD_RUN_FINISHED)


def _prs_ready(fakes: FakeActivities) -> list[NotifyInput]:
    return [spec for name, spec in fakes.calls if name == NOTIFY and spec.kind == NotificationKind.PRS_READY]


async def test_an_auto_merge_change_set_is_merged_with_the_project_method_and_no_one_is_asked() -> None:
    fakes = FakeActivities()
    run = run_input().model_copy(update={"merge_method": "rebase"})
    result, _ = await run_standard(fakes, "run-auto-merge", run)

    assert (result.outcome, result.prs) == (RunOutcome.MERGED, [PR])
    assert finished(fakes) == RunOutcome.MERGED
    (merged,) = fakes.of(MERGE_CHANGE_SET)
    assert (merged.prs, merged.method) == ([PR], "rebase")
    assert not fakes.of(WAIT_FOR_PR_CLOSED)
    assert not fakes.of(NOTIFY)  # neither `prs_ready` nor `run_finished`
    assert len(fakes.of(TEARDOWN_WORKSPACE)) == 1


async def test_a_human_review_change_set_is_not_merged_and_notifies_its_reasons() -> None:
    fakes = FakeActivities(signals=[SCHEMA])
    result, _ = await run_standard(fakes, "run-human-review")

    assert not fakes.of(MERGE_CHANGE_SET)
    (labelled,) = fakes.of(LABEL_CHANGE_SET)  # labelled `fact:needs-review` by classify, nothing to relabel
    assert labelled.change_set.classification.route == "human_review"
    assert labelled.change_set.classification.signals == [SCHEMA]
    (ready,) = _prs_ready(fakes)
    assert "changes the database schema" in ready.body
    names = [n for n, _ in fakes.calls]
    assert names.index(NOTIFY) < names.index(WAIT_FOR_PR_CLOSED)
    assert result.outcome == RunOutcome.MERGED  # the person merged it


async def test_a_refused_merge_falls_back_to_needs_review_and_notifies() -> None:
    fakes = FakeActivities(merges=False)
    result, _ = await run_standard(fakes, "run-merge-refused")

    first, relabel = fakes.of(LABEL_CHANGE_SET)
    assert first.change_set.classification.route == "auto_merge"
    assert relabel.change_set.classification.route == "human_review"
    assert relabel.change_set.classification.reasons == [MERGE_REFUSED]
    (ready,) = _prs_ready(fakes)
    assert MERGE_REFUSED in ready.body
    names = [n for n, _ in fakes.calls]
    assert names.index(MERGE_CHANGE_SET) < names.index(NOTIFY) < names.index(WAIT_FOR_PR_CLOSED)
    assert result.outcome == RunOutcome.MERGED  # the person merged it


async def test_auto_merge_off_relabels_for_review_without_merging() -> None:
    fakes = FakeActivities()
    result, _ = await run_standard(fakes, "run-auto-merge-off", run_input().model_copy(update={"auto_merge": False}))

    assert not fakes.of(MERGE_CHANGE_SET)
    assert fakes.of(LABEL_CHANGE_SET)[-1].change_set.classification.route == "human_review"
    assert len(_prs_ready(fakes)) == 1
    assert result.outcome == RunOutcome.MERGED  # the person merged it
