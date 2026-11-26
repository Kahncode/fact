"""
Dropping a run, as a mixin of `FactWorkflow`: a person cancels the workflow (`fact drop`) to restart its task.

The run ends `dropped`: it writes a handoff note to the artifact store (the last stage reached, each agent step's
summary, the PR links), tears its workspace down so the task branch is free, tells the task's source, and records the
outcome last, so a run shown `dropped` already has its note. The remote branch and the PRs stay: the next run of the
task starts from that branch, reuses the open PRs and gets the note as `$handoff` in its prompts.

`DropSteps` uses the workflow's `input`, `prs`, `_stage`, `_stage_attempt`, `_control`, `_record`, `_teardown` and
`report_task`.
"""

import asyncio
from typing import TYPE_CHECKING

from temporalio import workflow
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from fact.core.activity_contract import (
        RECORD_RUN_FINISHED,
        WRITE_HANDOFF,
        HandoffInput,
        HandoffStep,
        RunFinishedInput,
    )
    from fact.core.enums import RunOutcome
    from fact.core.types import SUMMARY_MAX_BYTES, RunResult, StepResult, limit_text

if TYPE_CHECKING:
    from fact.sdk.workflow import FactWorkflow


class DropSteps:
    def __init__(self) -> None:
        self._agent_steps: list[HandoffStep] = []
        self._dropping = False

    @staticmethod
    def drop_requested() -> bool:
        """Return whether a person asked to drop the run: Temporal delivered a cancellation of the workflow."""
        return workflow.in_workflow() and workflow.cancellation_reason() is not None

    def stop_if_dropped(self) -> None:
        """
        Raise `CancelledError` before a new step of a dropped run, so no step starts after the drop.

        A workflow that catches the error of a cancelled activity (such as a `learn` that may fail) would go on.
        """
        if not self._dropping and self.drop_requested():
            raise asyncio.CancelledError

    def note_agent_step(self, stage_id: str, attempt: int, result: StepResult) -> None:
        self._agent_steps.append(
            HandoffStep(stage_id=stage_id, attempt=attempt, outcome=result.outcome, summary=result.summary)
        )

    async def drop(self: FactWorkflow) -> RunResult:
        """End the dropped run: handoff note, teardown, then the `dropped` outcome; every step is best effort."""
        self._dropping = True
        reason = workflow.cancellation_reason() or ""
        run_id = self.input.run_id
        workflow.logger.info("run %s dropped: %s", run_id, reason or "no reason given")
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
        await self.report_task("dropped", detail=reason)
        await self._record(RECORD_RUN_FINISHED, RunFinishedInput(run_id=run_id, outcome=RunOutcome.DROPPED))
        summary = f"dropped: {reason}" if reason else "dropped"
        return RunResult(outcome=RunOutcome.DROPPED, prs=self.prs, summary=limit_text(summary, SUMMARY_MAX_BYTES))
