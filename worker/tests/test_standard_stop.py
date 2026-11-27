"""Stopping a `Standard` run: it ends `stopped` with a handoff note and no workspace; a restart gets the note."""

import asyncio

from fact.core.activity_contract import (
    AGENT_STEP,
    OPEN_CHANGE_SET,
    POST_COST_COMMENT,
    RECORD_RUN_FINISHED,
    REPORT_STATUS,
    TEARDOWN_WORKSPACE,
    WAIT_FOR_PR_CLOSED,
    WRITE_HANDOFF,
    AgentStepInput,
    PRState,
    WaitForPRClosedInput,
)
from fact.core.enums import NotificationKind, RunOutcome, StepOutcome
from fact.core.types import RunResult, StepResult
from fact.sdk.pull_requests import REVIEW_STAGE
from fact.worker.loader import sandbox_runner
from gate_harness import POLL_S, open_gate, until_status
from standard_harness import STANDARD, FakeActivities, finished, run_standard, started_standard
from standard_inputs import PR, run_input
from temporalio import activity
from temporalio.client import WorkflowHandle, WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer

HANDOFF = "runs/run-0/handoff.md"


class ReviewWaits(FakeActivities):
    """Nobody merges the PR: `wait_for_pr_closed` heartbeats until it is cancelled."""

    @activity.defn(name=WAIT_FOR_PR_CLOSED)
    async def wait_for_pr_closed(self, spec: WaitForPRClosedInput) -> list[PRState]:
        self._call(WAIT_FOR_PR_CLOSED, spec)
        while True:
            activity.heartbeat()
            await asyncio.sleep(POLL_S)


class AgentWorks(FakeActivities):
    """The implement session runs until it is cancelled, then records that it stopped."""

    @activity.defn(name=AGENT_STEP)
    async def agent_step(self, spec: AgentStepInput) -> StepResult:
        self._call(AGENT_STEP, spec)
        try:
            while True:
                activity.heartbeat()
                await asyncio.sleep(POLL_S)
        finally:
            self.calls.append(("session_stopped", spec.stage_id))


async def _at_stage(handle: WorkflowHandle, stage: str) -> None:
    await until_status(handle, lambda s: s.stage == stage)


def _before(fakes: FakeActivities, *names: str) -> bool:
    seen = [n for n, _ in fakes.calls]  # True when each call ran, in the order given
    return all(n in seen for n in names) and [seen.index(n) for n in names] == sorted(seen.index(n) for n in names)


async def test_stop_at_a_gate_leaves_a_handoff_and_no_workspace() -> None:
    fakes = FakeActivities(test_failures=100, gate_decisions=None)
    async with started_standard(fakes, "run-stop-gate") as (_, handle):
        await open_gate(handle)
        await handle.cancel(reason="restart on the new prompts")
        result: RunResult = await handle.result()

    assert (result.outcome, result.summary, result.prs) == (
        RunOutcome.STOPPED,
        "stopped: restart on the new prompts",
        [],
    )
    assert finished(fakes) == RunOutcome.STOPPED
    assert _before(fakes, WRITE_HANDOFF, TEARDOWN_WORKSPACE)  # the note is written before the workspace goes
    (note,) = fakes.of(WRITE_HANDOFF)
    assert (note.run_id, note.reason, note.stage_id, note.attempt) == (
        "run-1",
        "restart on the new prompts",
        "needs_human",
        1,
    )
    assert [(s.stage_id, s.outcome) for s in note.steps][:2] == [("implement", "passed"), ("implement", "passed")]
    assert note.steps[0].summary == "implement done"
    assert fakes.of(OPEN_CHANGE_SET) == []
    notified = [n.kind for n in fakes.of("notify") if n.kind != NotificationKind.GATE_WAITING]
    assert sorted(notified) == [NotificationKind.RUN_FINISHED, NotificationKind.TASK_HELD]
    reports = [(r.event, r.detail) for r in fakes.of(REPORT_STATUS)]
    assert reports[-1] == ("stopped", "restart on the new prompts")  # the task is held, said why
    assert [e for e, _ in reports].count("gate_opened") == 1  # the gate was never answered


async def test_stop_in_review_keeps_the_pr_for_the_next_run() -> None:
    fakes = ReviewWaits(merges=False)
    async with started_standard(fakes, "run-stop-review") as (_, handle):
        await _at_stage(handle, REVIEW_STAGE)
        await handle.cancel(reason="")
        result: RunResult = await handle.result()
        history = await handle.fetch_history()

    assert (result.outcome, result.summary, result.prs) == (RunOutcome.STOPPED, "stopped", [PR])
    (note,) = fakes.of(WRITE_HANDOFF)
    assert (note.stage_id, note.prs, note.reason) == (REVIEW_STAGE, [PR], "")
    assert [s.stage_id for s in note.steps] == ["implement", "review", "learn", "classify"]
    # the cost comment goes on the kept PR last, once the stop is recorded
    assert _before(fakes, WRITE_HANDOFF, TEARDOWN_WORKSPACE)
    assert _before(fakes, RECORD_RUN_FINISHED, POST_COST_COMMENT)
    (comment,) = fakes.of(POST_COST_COMMENT)
    assert comment.prs == [PR]

    replayed = WorkflowHistory.from_json("run-stop-review", history.to_json())
    replayer = Replayer(workflows=[STANDARD], data_converter=pydantic_data_converter, workflow_runner=sandbox_runner())
    await replayer.replay_workflow(replayed)


async def test_restart_hands_the_note_to_every_agent_session() -> None:
    fakes = FakeActivities()
    result, _ = await run_standard(fakes, "run-restart", run_input().model_copy(update={"handoff_ref": HANDOFF}))

    assert result.outcome == RunOutcome.MERGED
    sessions = fakes.of(AGENT_STEP)
    assert [s.prompt_name for s in sessions] == ["implement", "review", "learn", "classify"]
    assert {s.handoff_ref for s in sessions} == {HANDOFF}
    assert fakes.of(WRITE_HANDOFF) == []


async def test_an_ordinary_run_has_no_handoff() -> None:
    fakes = FakeActivities(implement_outcome=StepOutcome.NEEDS_HUMAN)
    await run_standard(fakes, "run-no-handoff")
    assert {s.handoff_ref for s in fakes.of(AGENT_STEP)} == {None}
    assert fakes.of(WRITE_HANDOFF) == []


async def test_stop_during_a_session_tears_down_once_it_stopped() -> None:
    fakes = AgentWorks()
    async with started_standard(fakes, "run-stop-agent") as (_, handle):
        await _at_stage(handle, "implement")
        await handle.cancel(reason="wrong approach")
        result: RunResult = await handle.result()

    assert (result.outcome, result.summary) == (RunOutcome.STOPPED, "stopped: wrong approach")
    assert _before(fakes, "session_stopped", WRITE_HANDOFF, TEARDOWN_WORKSPACE)  # nothing writes once torn down
    (note,) = fakes.of(WRITE_HANDOFF)
    assert (note.stage_id, note.attempt, note.steps) == ("implement", 1, [])
