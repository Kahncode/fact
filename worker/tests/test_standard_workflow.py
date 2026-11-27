"""`.fact/workflows/standard.py` against fake activities registered under the real names, replaying the green run's history."""

import pytest
from fact.core.activity_contract import (
    AGENT_INTERRUPTED,
    AGENT_STEP,
    CLASSIFY_SIGNALS,
    LABEL_CHANGE_SET,
    MARK_TASK_DONE,
    MERGE_CHANGE_SET,
    NOTIFY,
    OPEN_CHANGE_SET,
    POST_COST_COMMENT,
    PREPARE_WORKSPACE,
    RECORD_GATE_OPENED,
    RECORD_RUN_STARTED,
    REPORT_STATUS,
    SHELL_STEP,
    TEARDOWN_WORKSPACE,
    WAIT_FOR_CI,
)
from fact.core.enums import RunOutcome, StepOutcome
from fact.core.types import StepResult
from fact.sdk.sessions import MAX_RESUMES
from fact.worker.loader import sandbox_runner
from standard_harness import (
    MAX_FIX_ATTEMPTS,
    STANDARD,
    FakeActivities,
    agent_sessions,
    checks,
    finished,
    run_standard,
)
from standard_inputs import BRANCH, LEARNED, MET, PR, RUN_URL
from temporalio.client import WorkflowFailureError, WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer


async def test_green_path() -> None:
    fakes = FakeActivities()
    result, history = await run_standard(fakes, "run-green")

    assert result.outcome == RunOutcome.MERGED
    assert result.prs == [PR]
    order = [n for n, _ in fakes.calls]
    assert order[0] == RECORD_RUN_STARTED
    assert order.index(PREPARE_WORKSPACE) < order.index(AGENT_STEP)
    assert order.index(MARK_TASK_DONE) < order.index(OPEN_CHANGE_SET) < order.index(WAIT_FOR_CI)
    assert order.index(WAIT_FOR_CI) < order.index(CLASSIFY_SIGNALS) < order.index(LABEL_CHANGE_SET)
    assert order.index(LABEL_CHANGE_SET) < order.index(MERGE_CHANGE_SET)
    assert order[-3:] == [TEARDOWN_WORKSPACE, POST_COST_COMMENT, NOTIFY]  # the cost comment covers every step
    assert [(r.task_key, r.event) for r in fakes.of(REPORT_STATUS)] == [
        ("task:M1-99", "run_started"),
        ("task:M1-99", "prs_opened"),
        ("task:M1-99", "merged"),
    ]
    assert fakes.of(REPORT_STATUS)[1].prs == [PR]
    assert agent_sessions(fakes) == [
        ("implement", 1, "implement"),
        ("review", 1, "review"),
        ("learn", 1, "learn"),
        ("classify", 1, "classify"),  # once CI is green
    ]
    agents = [i for i, n in enumerate(order) if n == AGENT_STEP]
    assert agents[2] < order.index(MARK_TASK_DONE) < order.index(WAIT_FOR_CI) < agents[3]  # learn, then classify
    (labelled,) = fakes.of(LABEL_CHANGE_SET)
    assert (labelled.stage_id, labelled.prs) == ("label_prs", [PR])
    assert labelled.change_set.classification.route == "auto_merge"
    assert [(s.stage_id, s.attempt, s.check) for s in fakes.of(SHELL_STEP)] == [
        ("setup", 1, "setup"),
        ("checks", 1, "lint"),
        ("checks", 2, "test"),
    ]
    (pr_spec,) = fakes.of(OPEN_CHANGE_SET)
    assert pr_spec.branch == BRANCH
    assert (pr_spec.title, pr_spec.summary) == (LEARNED["title"], LEARNED["summary"])  # from the learn session
    assert pr_spec.run_url == RUN_URL
    assert [(c.name, c.outcome) for c in pr_spec.check_results] == [("lint", "passed"), ("test", "passed")]
    assert [c.model_dump() for c in pr_spec.criteria] == [MET]  # a low finding needs no address_review
    assert pr_spec.attempt == 1
    (ci_spec,) = fakes.of(WAIT_FOR_CI)
    assert (ci_spec.stage_id, ci_spec.attempt, ci_spec.pr) == ("wait_for_ci", 1, PR)
    assert [(c.run_id, c.prs) for c in fakes.of(POST_COST_COMMENT)] == [("run-1", [PR])]
    assert finished(fakes) == RunOutcome.MERGED

    # Replay through JSON: the code is deterministic and the history serializes with our converter.
    replayed = WorkflowHistory.from_json("run-green", history.to_json())
    replayer = Replayer(workflows=[STANDARD], data_converter=pydantic_data_converter, workflow_runner=sandbox_runner())
    await replayer.replay_workflow(replayed)


async def test_two_failures_then_green() -> None:
    fakes = FakeActivities(test_failures=2)
    result, _ = await run_standard(fakes, "run-two-failures")

    assert result.outcome == RunOutcome.MERGED
    assert agent_sessions(fakes) == [
        ("implement", 1, "implement"),
        ("implement", 2, "fix_checks"),
        ("implement", 3, "fix_checks"),
        ("review", 1, "review"),
        ("learn", 1, "learn"),
        ("classify", 1, "classify"),
    ]
    fix = fakes.of(AGENT_STEP)[1]
    assert "### test: `pytest` exited with 1" in fix.context
    assert "FAILED test_widget" in fix.context
    assert [s.attempt for s in checks(fakes)] == [1, 2, 3, 4, 5, 6]  # three rounds of lint and test
    assert len(fakes.of(OPEN_CHANGE_SET)) == 1


async def test_exhausted_checks_need_a_human() -> None:
    fakes = FakeActivities(test_failures=100)
    result, _ = await run_standard(fakes, "run-exhausted")

    assert result.outcome == RunOutcome.NEEDS_HUMAN
    assert result.prs == []
    assert "test" in result.summary
    assert len(fakes.of(AGENT_STEP)) == 1 + MAX_FIX_ATTEMPTS
    assert len(checks(fakes)) == 2 * (MAX_FIX_ATTEMPTS + 1)
    assert fakes.of(MARK_TASK_DONE) == []
    assert fakes.of(OPEN_CHANGE_SET) == []
    assert fakes.of(POST_COST_COMMENT) == []  # no PR to comment on
    assert finished(fakes) == RunOutcome.NEEDS_HUMAN
    assert fakes.of(TEARDOWN_WORKSPACE) == []  # left on disk, holding its branch and commits
    (gate,) = fakes.gates(RECORD_GATE_OPENED)  # a person was asked first, and rejected
    assert (gate.gate_id, gate.kind) == ("checks-1", "needs_human")


async def test_pr_goes_on_the_branch_the_workspace_checked_out() -> None:
    fakes = FakeActivities(checked_out=f"{BRANCH}-2")
    result, _ = await run_standard(fakes, "run-branch-taken")

    assert result.outcome == RunOutcome.MERGED
    (pr_spec,) = fakes.of(OPEN_CHANGE_SET)
    assert pr_spec.branch == f"{BRANCH}-2"


@pytest.mark.parametrize(
    ("fakes", "outcome", "agent_steps"),
    [
        (FakeActivities(setup_outcome=StepOutcome.FAILED_CHECKS), RunOutcome.FAILED, 0),
        (FakeActivities(implement_outcome=StepOutcome.FAILED_ERROR), RunOutcome.FAILED, 1),
        (FakeActivities(implement_outcome=StepOutcome.NEEDS_HUMAN), RunOutcome.NEEDS_HUMAN, 1),
    ],
    ids=["setup_failed", "implement_failed", "implement_blocked"],
)
async def test_run_ending_before_checks_opens_no_pr(
    fakes: FakeActivities, outcome: RunOutcome, agent_steps: int
) -> None:
    result, _ = await run_standard(fakes, "run-ends-early")

    assert result.outcome == outcome
    assert len(fakes.of(AGENT_STEP)) == agent_steps
    assert checks(fakes) == []
    assert fakes.of(MARK_TASK_DONE) == []
    assert fakes.of(OPEN_CHANGE_SET) == []
    assert finished(fakes) == outcome


RETRYABLE = StepResult(
    outcome=StepOutcome.NEEDS_HUMAN,
    summary="rate limited",
    outputs={"status": "blocked", "retryable": True, "blocker": "API rate limit"},
    session_id="s-1",
)
INTERRUPTED = ApplicationError("worker stopped", "s-1", type=AGENT_INTERRUPTED, non_retryable=True)


async def test_retryable_block_resumes_the_session_once() -> None:
    fakes = FakeActivities(implement_script=[RETRYABLE])
    result, _ = await run_standard(fakes, "run-retryable")

    assert result.outcome == RunOutcome.MERGED
    first, second, _review, _learn, _classify = fakes.of(AGENT_STEP)
    assert (first.attempt, first.resume_session_id) == (1, None)
    assert (second.stage_id, second.attempt, second.prompt_name) == ("implement", 2, "implement")
    assert second.resume_session_id == "s-1"
    assert "API rate limit" in second.context


async def test_retryable_block_twice_needs_a_human() -> None:
    fakes = FakeActivities(implement_script=[RETRYABLE, RETRYABLE])
    result, _ = await run_standard(fakes, "run-retryable-twice")

    assert result.outcome == RunOutcome.NEEDS_HUMAN
    assert len(fakes.of(AGENT_STEP)) == MAX_RESUMES + 1  # one resume, never more
    assert fakes.of(OPEN_CHANGE_SET) == []


async def test_interrupted_worker_resumes_the_session() -> None:
    fakes = FakeActivities(implement_script=[INTERRUPTED])
    result, _ = await run_standard(fakes, "run-interrupted")

    assert result.outcome == RunOutcome.MERGED
    _, resumed, _review, _learn, _classify = fakes.of(AGENT_STEP)
    assert (resumed.attempt, resumed.resume_session_id) == (2, "s-1")
    assert "worker" in resumed.context


async def test_interrupted_twice_fails_the_run() -> None:
    fakes = FakeActivities(implement_script=[INTERRUPTED, INTERRUPTED])
    with pytest.raises(WorkflowFailureError):
        await run_standard(fakes, "run-interrupted-twice")
    assert len(fakes.of(AGENT_STEP)) == MAX_RESUMES + 1
    assert finished(fakes) == RunOutcome.FAILED
    assert fakes.of(TEARDOWN_WORKSPACE) == []


@pytest.mark.parametrize("broken", [TEARDOWN_WORKSPACE, POST_COST_COMMENT, RECORD_RUN_STARTED, REPORT_STATUS, NOTIFY])
async def test_best_effort_activity_failure_does_not_fail_the_run(broken: str) -> None:
    fakes = FakeActivities(broken=broken)
    result, _ = await run_standard(fakes, "run-best-effort")
    assert result.outcome == RunOutcome.MERGED


async def test_failing_activity_fails_the_run_and_records_it() -> None:
    fakes = FakeActivities(broken=PREPARE_WORKSPACE)
    with pytest.raises(WorkflowFailureError):
        await run_standard(fakes, "run-prepare-broken")
    assert finished(fakes) == RunOutcome.FAILED
