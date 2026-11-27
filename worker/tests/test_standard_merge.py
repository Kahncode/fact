"""`Standard` once CI is green: tell a person, wait until the PRs merge or close, end that way, tear down."""

import pytest
from fact.core.activity_contract import (
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
)
from fact.core.enums import NotificationKind, RunOutcome, StepOutcome
from fact.core.gates import GateDecision
from fact.sdk.pull_requests import REVIEW_STAGE
from standard_harness import FakeActivities, finished, run_standard
from standard_inputs import PR
from temporalio.client import WorkflowFailureError

ENDS = [
    ("merged", RunOutcome.MERGED, GateDecision.APPROVE, StepOutcome.PASSED),
    ("closed", RunOutcome.CLOSED, GateDecision.REJECT, StepOutcome.NEEDS_HUMAN),
]


@pytest.mark.parametrize(("state", "outcome", "decision", "step_outcome"), ENDS)
async def test_run_ends_as_its_pr_did_and_tears_down(
    state: str, outcome: RunOutcome, decision: GateDecision, step_outcome: StepOutcome
) -> None:
    fakes = FakeActivities(pr_state=state)  # type: ignore[arg-type]
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
    fakes = FakeActivities()
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
    fakes = FakeActivities(broken=WAIT_FOR_PR_CLOSED)
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


async def test_merge_when_green_ends_merged_without_a_person() -> None:
    fakes = FakeActivities(merges=True)
    result, _ = await run_standard(fakes, "run-merge-when-green")

    assert (result.outcome, result.prs) == (RunOutcome.MERGED, [PR])
    assert finished(fakes) == RunOutcome.MERGED
    (merged,) = fakes.of(MERGE_CHANGE_SET)
    assert (merged.prs, merged.method) == ([PR], "squash")
    assert not fakes.of(WAIT_FOR_PR_CLOSED)
    assert not [spec for name, spec in fakes.calls if name == NOTIFY and spec.kind == NotificationKind.PRS_READY]
    assert len(fakes.of(TEARDOWN_WORKSPACE)) == 1


async def test_a_refused_merge_falls_back_to_waiting_for_a_person() -> None:
    fakes = FakeActivities(merges=False)
    result, _ = await run_standard(fakes, "run-merge-refused")

    names = [n for n, _ in fakes.calls]
    assert names.index(MERGE_CHANGE_SET) < names.index(WAIT_FOR_PR_CLOSED)
    assert result.outcome == RunOutcome.MERGED  # the person merged it
