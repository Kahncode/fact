"""`Standard` notifies a person: once per gate it waits at, when its PRs are ready, and when it ends badly."""

import pytest
from fact.core.activity_contract import NOTIFY, PREPARE_WORKSPACE, NotifyInput
from fact.core.enums import NotificationKind, RunOutcome, StepOutcome
from fact.core.gates import GateDecision
from fact.core.types import StepResult
from standard_harness import FakeActivities, finished, run_standard
from standard_inputs import PR, RUN_URL
from standard_question_fakes import blocked
from temporalio.client import WorkflowFailureError

APPROVE = GateDecision.APPROVE


def _sent(fakes: FakeActivities) -> list[tuple[NotificationKind, str]]:
    return [(n.kind, n.dedupe_key) for n in fakes.of(NOTIFY)]


async def test_a_change_set_fact_cannot_merge_notifies_its_prs_are_ready_then_merged() -> None:
    fakes = FakeActivities(merges=False)
    await run_standard(fakes, "run-notify-green")
    ready, ended = fakes.of(NOTIFY)
    assert (ready.kind, ready.dedupe_key, ready.link) == (NotificationKind.PRS_READY, "run-1:prs_ready", PR.url)
    assert (ended.kind, ended.link) == (NotificationKind.RUN_FINISHED, PR.url)


async def test_each_gate_notifies_once_then_the_end() -> None:
    fakes = FakeActivities(implement_script=[blocked("v1 or v2?"), blocked("Colour?")], gate_decisions=[APPROVE])
    result, _ = await run_standard(fakes, "run-notify-gates")

    assert result.outcome == RunOutcome.NEEDS_HUMAN  # the second gate is rejected
    assert _sent(fakes) == [
        (NotificationKind.GATE_WAITING, "run-1:gate:implement-question-1"),
        (NotificationKind.GATE_WAITING, "run-1:gate:implement-question-2"),
        (NotificationKind.TASK_HELD, "run-1:held"),  # needs_human holds the task
        (NotificationKind.RUN_FAILED, "run-1:run_failed"),
    ]
    gate: NotifyInput = fakes.of(NOTIFY)[0]
    assert gate.link == RUN_URL
    assert "run-1" in gate.body  # how to answer it
    ended: NotifyInput = fakes.of(NOTIFY)[-1]
    assert ended.link == RUN_URL  # no PR to point at


async def test_blocked_agent_notifies_the_run_failed_and_its_task_held() -> None:
    blocked = StepResult(outcome=StepOutcome.NEEDS_HUMAN, summary="needs a key")
    fakes = FakeActivities(implement_script=[blocked])
    await run_standard(fakes, "run-notify-blocked")
    assert _sent(fakes) == [
        (NotificationKind.TASK_HELD, "run-1:held"),
        (NotificationKind.RUN_FAILED, "run-1:run_failed"),
    ]
    held: NotifyInput = fakes.of(NOTIFY)[0]
    assert "needs a person" in held.title
    assert "fact run" in held.body  # how to release it


async def test_failed_run_notifies_before_failing() -> None:
    fakes = FakeActivities(broken=PREPARE_WORKSPACE)
    with pytest.raises(WorkflowFailureError):
        await run_standard(fakes, "run-notify-broken")
    assert finished(fakes) == RunOutcome.FAILED
    (failed,) = fakes.of(NOTIFY)
    assert failed.kind == NotificationKind.RUN_FAILED
    assert "prepare_workspace is broken" in failed.body
