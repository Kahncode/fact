"""`Standard` notifies a person: once per gate it waits at, when its PRs are ready, and when it ends badly."""

import pytest
from fact.core.activity_contract import NOTIFY, PREPARE_WORKSPACE, NotifyInput
from fact.core.enums import NotificationKind, RunOutcome, StepOutcome
from fact.core.gates import GateDecision
from fact.core.types import StepResult
from standard_harness import MAX_FIX_ATTEMPTS, FakeActivities, finished, run_standard
from standard_inputs import PR, RUN_URL
from temporalio.client import WorkflowFailureError

ROUNDS = MAX_FIX_ATTEMPTS + 1  # check rounds in one `until_green`


def _sent(fakes: FakeActivities) -> list[tuple[NotificationKind, str]]:
    return [(n.kind, n.dedupe_key) for n in fakes.of(NOTIFY)]


async def test_green_run_notifies_its_prs_are_ready_then_merged() -> None:
    fakes = FakeActivities()
    await run_standard(fakes, "run-notify-green")
    ready, ended = fakes.of(NOTIFY)
    assert (ready.kind, ready.dedupe_key, ready.link) == (NotificationKind.PRS_READY, "run-1:prs_ready", PR.url)
    assert ready.title == "task:M1-99: 1 PR(s) ready for review"
    assert ready.body == PR.url
    assert (ended.kind, ended.title, ended.link) == (NotificationKind.RUN_FINISHED, "task:M1-99 ended merged", PR.url)


async def test_each_gate_notifies_once_then_the_end() -> None:
    fakes = FakeActivities(test_failures=2 * ROUNDS, gate_decisions=[GateDecision.APPROVE])
    result, _ = await run_standard(fakes, "run-notify-gates")

    assert result.outcome == RunOutcome.NEEDS_HUMAN  # the second gate is rejected
    assert _sent(fakes) == [
        (NotificationKind.GATE_WAITING, "run-1:gate:checks-1"),
        (NotificationKind.GATE_WAITING, "run-1:gate:checks-2"),
        (NotificationKind.RUN_FAILED, "run-1:run_failed"),
    ]
    gate: NotifyInput = fakes.of(NOTIFY)[0]
    assert gate.title == "task:M1-99 waits for you at gate checks-1 (needs_human)"
    assert gate.link == RUN_URL
    assert "test" in gate.body
    assert gate.body.endswith("Answer with `fact gate run-1 approve|reject`.")
    ended: NotifyInput = fakes.of(NOTIFY)[-1]
    assert ended.title == "task:M1-99 ended needs_human"
    assert ended.link == RUN_URL  # no PR to point at


async def test_blocked_agent_notifies_the_run_failed() -> None:
    blocked = StepResult(outcome=StepOutcome.NEEDS_HUMAN, summary="needs a key")
    fakes = FakeActivities(implement_script=[blocked])
    await run_standard(fakes, "run-notify-blocked")
    assert _sent(fakes) == [(NotificationKind.RUN_FAILED, "run-1:run_failed")]


async def test_failed_run_notifies_before_failing() -> None:
    fakes = FakeActivities(broken=PREPARE_WORKSPACE)
    with pytest.raises(WorkflowFailureError):
        await run_standard(fakes, "run-notify-broken")
    assert finished(fakes) == RunOutcome.FAILED
    (failed,) = fakes.of(NOTIFY)
    assert (failed.kind, failed.title) == (NotificationKind.RUN_FAILED, "task:M1-99 ended failed")
    assert "prepare_workspace is broken" in failed.body


async def test_notify_failure_does_not_fail_the_run() -> None:
    fakes = FakeActivities(broken=NOTIFY, test_failures=ROUNDS, gate_decisions=[GateDecision.APPROVE])
    result, _ = await run_standard(fakes, "run-notify-down")
    assert result.outcome == RunOutcome.MERGED
    kinds = [n.kind for n in fakes.of(NOTIFY)]
    assert kinds == [NotificationKind.GATE_WAITING, NotificationKind.PRS_READY, NotificationKind.RUN_FINISHED]
