"""
Stopping a run, as a mixin of `FactWorkflow`: a person cancels the workflow (`fact stop`) to restart its task.

The run ends `stopped`: it writes a handoff note to the artifact store (the last stage reached, each agent step's
summary, the PR links), tears its workspace down so the task branch is free, tells the task's source, and records the
outcome, so a run shown `stopped` already has its note, and comments the run's cost on its PRs last. The remote branch and the PRs stay: the next run of the
task starts from that branch, reuses the open PRs and gets the note as `$handoff` in its prompts.

`StopSteps` uses the workflow's `input`, `prs`, `_stage`, `_stage_attempt`, `_control`, `_teardown`, `report_end`
and `record_run_end`.
"""

import asyncio
from typing import TYPE_CHECKING

from temporalio import workflow
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from fact.core.activity_contract import (
        WRITE_HANDOFF,
        HandoffInput,
        HandoffStep,
    )
    from fact.core.enums import RunOutcome
    from fact.core.types import SUMMARY_MAX_BYTES, RunResult, StepResult, limit_text

if TYPE_CHECKING:
    from fact.sdk.workflow import FactWorkflow


class StopSteps:
    def __init__(self) -> None:
        self._agent_steps: list[HandoffStep] = []
        self._stopping = False

    @staticmethod
    def stop_requested() -> bool:
        """Return whether a person asked to stop the run: Temporal delivered a cancellation of the workflow."""
        return workflow.in_workflow() and workflow.cancellation_reason() is not None

    def raise_if_stopped(self) -> None:
        """
        Raise `CancelledError` before a new step of a stopped run, so no step starts after the stop.

        A workflow that catches the error of a cancelled activity (such as a `learn` that may fail) would go on.
        """
        if not self._stopping and self.stop_requested():
            raise asyncio.CancelledError

    def note_agent_step(self, stage_id: str, attempt: int, result: StepResult) -> None:
        self._agent_steps.append(
            HandoffStep(stage_id=stage_id, attempt=attempt, outcome=result.outcome, summary=result.summary)
        )

    async def stop(self: FactWorkflow) -> RunResult:
        """End the stopped run: handoff note, teardown, then the `stopped` outcome; every step is best effort."""
        self._stopping = True
        reason = workflow.cancellation_reason() or ""
        run_id = self.input.run_id
        workflow.logger.info("run %s stopped: %s", run_id, reason or "no reason given")
        note = HandoffInput(
            run_id=run_id,
            task=self.input.task,
            reason=reason,
            stage_id=self._stage,
            attempt=self._stage_attempt,
            steps=list(self._agent_steps),
            prs=list(self.prs),
        )
        try:
            await self._control(WRITE_HANDOFF, note, str)
        except ActivityError as exc:
            workflow.logger.warning("handoff note failed for run %s: %s", run_id, exc.cause or exc)
        await self._teardown()
        await self.report_end("stopped", reason)
        await self.record_run_end(RunOutcome.STOPPED)  # and the cost comment, if a PR was opened
        summary = f"stopped: {reason}" if reason else "stopped"
        return RunResult(outcome=RunOutcome.STOPPED, prs=self.prs, summary=limit_text(summary, SUMMARY_MAX_BYTES))
