"""
The run's task, as a mixin of `FactWorkflow`: tell its source what the run is doing, and mark it done on the branch.

Each event goes through the `report_status` control activity, which applies it as `fact.core.tasks.events` says
(labels, comments, PR links, closing the issue). Reporting is best effort: a tracker outage must not fail a run.

`TaskStatusSteps` uses the workflow's `input`, `handle`, `raise_if_stopped` and `_control`.
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
    from fact.core.enums import RunOutcome
    from fact.core.types import PRRef, RunResult

if TYPE_CHECKING:
    from fact.sdk.workflow import FactWorkflow

# how a run's end reaches its task: merged closes it, closed or stopped clears its labels; other ends say nothing yet
END_EVENTS: dict[RunOutcome, TaskEvent] = {
    RunOutcome.MERGED: "merged",
    RunOutcome.CLOSED: "closed",
    RunOutcome.STOPPED: "stopped",
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
        """Report a `merged`, `closed` or `stopped` end, with the run's summary; other outcomes report nothing."""
        if (event := END_EVENTS.get(result.outcome)) is not None:
            await self.report_task(event, detail=result.summary)

    async def mark_task_done(self: FactWorkflow) -> bool:
        """Record the task's completion in a commit on the branch (a file task's file); False when nothing to do."""
        run = self.input
        spec = MarkTaskDoneInput(run_id=run.run_id, task=run.task, handle=self.handle, home_repo=run.home_repo)
        return await self._control(MARK_TASK_DONE, spec, bool)
