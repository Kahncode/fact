"""`Standard` after the PR opens: wait for CI, fix sessions with the failing jobs, push again, give up to a person."""

from fact.core.activity_contract import (
    AGENT_STEP,
    NOTIFY,
    OPEN_CHANGE_SET,
    POST_COST_COMMENT,
    RECORD_GATE_ANSWERED,
    RECORD_GATE_OPENED,
    TEARDOWN_WORKSPACE,
    WAIT_FOR_CI,
)
from fact.core.enums import RunOutcome, StepOutcome
from fact.core.gates import GateDecision
from standard_harness import FakeActivities, agent_sessions, finished, run_standard
from standard_inputs import PR

MAX_CI_FIX_ATTEMPTS = 2
COST_COMMENTS = 2  # after the PR opens, and at the end of the run
MERGED_COST_COMMENTS = 3  # after the PR opens, once CI is green, and once it is merged


def _fix_ci_sessions(fakes: FakeActivities) -> list[tuple[str, int, str]]:
    return [s for s in agent_sessions(fakes) if s[0] == "fix_ci"]


async def test_ci_failure_goes_to_a_fix_session_then_pushes_and_waits_again() -> None:
    fakes = FakeActivities(ci_failures=1)
    result, _ = await run_standard(fakes, "run-ci-fixed")

    assert result.outcome == RunOutcome.MERGED
    assert _fix_ci_sessions(fakes) == [("fix_ci", 1, "fix_ci")]
    fix = next(s for s in fakes.of(AGENT_STEP) if s.stage_id == "fix_ci")
    assert "### test (windows): failure on acme/widget#7 at abc123" in fix.context
    assert "https://ci/1" in fix.context
    assert "E boom" in fix.context

    pushes = fakes.of(OPEN_CHANGE_SET)
    assert [p.attempt for p in pushes] == [1, 2]  # one `open_pr` step per push
    assert pushes[1].title == pushes[0].title  # the same PR text, updated in place
    waits = fakes.of(WAIT_FOR_CI)
    assert [(w.attempt, w.pr.head_sha) for w in waits] == [(1, PR.head_sha), (2, "fix1")]  # CI on the new head
    assert result.prs == [PR.model_copy(update={"head_sha": "fix1"})]
    assert fakes.of(POST_COST_COMMENT)[-1].prs == result.prs
    tail = [n for n, _ in fakes.calls][-5:]
    assert tail == [POST_COST_COMMENT, "record_run_finished", "report_status", TEARDOWN_WORKSPACE, NOTIFY]


async def test_exhausted_ci_fixes_need_a_human_with_the_pr_open() -> None:
    fakes = FakeActivities(ci_failures=100)
    result, _ = await run_standard(fakes, "run-ci-exhausted")

    assert result.outcome == RunOutcome.NEEDS_HUMAN
    assert "CI not green" in result.summary
    assert result.prs == [PR.model_copy(update={"head_sha": f"fix{MAX_CI_FIX_ATTEMPTS}"})]
    assert len(_fix_ci_sessions(fakes)) == MAX_CI_FIX_ATTEMPTS
    assert len(fakes.of(WAIT_FOR_CI)) == MAX_CI_FIX_ATTEMPTS + 1
    assert len(fakes.of(OPEN_CHANGE_SET)) == MAX_CI_FIX_ATTEMPTS + 1
    assert len(fakes.of(POST_COST_COMMENT)) == COST_COMMENTS
    assert finished(fakes) == RunOutcome.NEEDS_HUMAN
    assert fakes.of(TEARDOWN_WORKSPACE) == []
    (gate,) = fakes.gates(RECORD_GATE_OPENED)  # a person was asked first, and rejected
    assert (gate.gate_id, gate.details["loop"]) == ("wait_for_ci-1", "wait_for_ci")
    assert "CI not green" in gate.details["summary"]


async def test_approve_waits_for_ci_and_fixes_once_more() -> None:
    fakes = FakeActivities(ci_failures=MAX_CI_FIX_ATTEMPTS + 1, gate_decisions=[GateDecision.APPROVE])
    result, _ = await run_standard(fakes, "run-ci-gate-approve")

    assert result.outcome == RunOutcome.MERGED
    (answered,) = fakes.gates(RECORD_GATE_ANSWERED)
    assert (answered.gate_id, answered.decision) == ("wait_for_ci-1", "approve")
    waits = fakes.of(WAIT_FOR_CI)
    assert len(waits) == MAX_CI_FIX_ATTEMPTS + 2  # the loop again, from the last pushed head
    assert waits[-1].pr.head_sha == f"fix{MAX_CI_FIX_ATTEMPTS}"
    assert result.prs == [PR.model_copy(update={"head_sha": f"fix{MAX_CI_FIX_ATTEMPTS}"})]
    assert len(fakes.of(POST_COST_COMMENT)) == MERGED_COST_COMMENTS


async def test_ci_that_never_finishes_needs_a_human_without_a_fix() -> None:
    fakes = FakeActivities(ci_timed_out=True)
    result, _ = await run_standard(fakes, "run-ci-stuck")

    assert result.outcome == RunOutcome.NEEDS_HUMAN
    assert _fix_ci_sessions(fakes) == []
    assert len(fakes.of(WAIT_FOR_CI)) == 1


async def test_blocked_ci_fix_stops_the_loop() -> None:
    fakes = FakeActivities(ci_failures=100, fix_ci_outcome=StepOutcome.NEEDS_HUMAN)
    result, _ = await run_standard(fakes, "run-ci-fix-blocked")

    assert result.outcome == RunOutcome.NEEDS_HUMAN
    assert len(_fix_ci_sessions(fakes)) == 1
    assert len(fakes.of(OPEN_CHANGE_SET)) == 1  # nothing new to push
    assert len(fakes.of(WAIT_FOR_CI)) == 1


async def test_ci_fix_without_a_new_commit_stops_the_loop() -> None:
    fakes = FakeActivities(ci_failures=100, fix_ci_commits=False)
    result, _ = await run_standard(fakes, "run-ci-fix-empty")

    assert result.outcome == RunOutcome.NEEDS_HUMAN
    assert len(_fix_ci_sessions(fakes)) == 1
    assert len(fakes.of(OPEN_CHANGE_SET)) == 1 + 1  # opened, then pushed again with nothing new
    assert len(fakes.of(WAIT_FOR_CI)) == 1  # CI on the same commit would fail the same way
