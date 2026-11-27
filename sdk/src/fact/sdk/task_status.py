"""
The run's task, as a mixin of `FactWorkflow`: tell its source what the run is doing, and mark it done on the branch.

Each event goes through the `report_status` control activity, which applies it as `fact.core.tasks.events` says
(labels, comments, PR links, closing the issue). Reporting is best effort: a tracker outage must not fail a run.

`TaskStatusSteps` uses the workflow's `input`, `handle`, `raise_if_stopped`, `notify` and `_control`. A run end that
holds the task (`fact.core.holds.HELD_ENDS`) also sends a `task_held` notification, once per run (`<run id>:held`).
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING

from temporalio import workflow
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from fact.core.activity_contract import (
        MARK_TASK_DONE,
        REPORT_STATUS,
        MarkTaskDoneInput,
        ReportStatusInput,
        TaskEvent,
    )
    from fact.core.enums import NotificationKind, RunOutcome
    from fact.core.holds import HELD_ENDS, held_text, held_title
    from fact.core.types import PRRef, RunResult

if TYPE_CHECKING:
    from fact.sdk.workflow import FactWorkflow

# how a run's end reaches its task: merged closes it; the others hold it (`fact:needs-human`, `task_held`); a failed
# end says nothing (the scheduler retries it, and its tick holds the task at the failure cap)
END_EVENTS: dict[RunOutcome, TaskEvent] = {
    RunOutcome.MERGED: "merged",
    RunOutcome.CLOSED: "closed",
    RunOutcome.STOPPED: "stopped",
    RunOutcome.NEEDS_HUMAN: "needs_human",
    RunOutcome.PR_OPENED: "pr_opened",
}


class TaskStatusSteps:
    async def report_task(self: FactWorkflow, event: TaskEvent, detail: str = "", prs: Sequence[PRRef] = ()) -> None:
        """Send `event` to the task's source, best effort: a failure is logged, and the run goes on."""
        run = self.input
        spec = ReportStatusInput(
            run_id=run.run_id,
            project=run.project,
            task_key=run.task.key,
            event=event,
            run_url=run.run_url,
            detail=detail,
            prs=list(prs),
        )
        try:
            await self._control(REPORT_STATUS, spec, type(None))
        except ActivityError as exc:
            self.raise_if_stopped()  # a stop that cancelled the report must still stop the run
            workflow.logger.warning("report_status %s failed for run %s: %s", event, run.run_id, exc.cause or exc)

    async def report_run_end(self: FactWorkflow, result: RunResult) -> None:
        """Report an end in `END_EVENTS`, with the run's summary; a `failed` one reports nothing."""
        if (event := END_EVENTS.get(result.outcome)) is not None:
            await self.report_end(event, result.summary)

    async def report_end(self: FactWorkflow, event: TaskEvent, detail: str) -> None:
        """Report the run's end; one that holds the task also sends one `task_held` notification (best effort)."""
        await self.report_task(event, detail=detail)
        if (reason := HELD_ENDS.get(event)) is not None:
            key = self.input.task.key
            await self.notify(NotificationKind.TASK_HELD, held_title(key, reason), held_text(key, reason), key="held")

    async def mark_task_done(self: FactWorkflow) -> bool:
        """Record the task's completion in a commit on the branch (a file task's file); False when nothing to do."""
        run = self.input
        spec = MarkTaskDoneInput(run_id=run.run_id, task=run.task, handle=self.handle, home_repo=run.home_repo)
        return await self._control(MARK_TASK_DONE, spec, bool)
