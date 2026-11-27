"""
What each run event does to the task in its source: labels, comments, PR links, status and closing.

The `report_status` activity calls `report`; a source that keeps no state of its own (files) only logs each write.
A run end the scheduler does not retry (`HELD_ENDS`) holds the task: `hold` adds `fact:needs-human`, keeps
`fact:ready`, and comments why and how to release it. No event removes `fact:needs-human` but a gate's answer.
Only `run_started` (to in progress) and `prs_opened` (to in review) move the status, after the event's other writes.
"""

from fact.core.activity_contract import ReportStatusInput
from fact.core.holds import HELD_ENDS, NEEDS_HUMAN, held_text
from fact.core.tasks.base import TaskSource, TaskStatus

__all__ = ["NEEDS_HUMAN", "RUNNING", "hold", "report"]

RUNNING = "fact:running"


def _run_link(spec: ReportStatusInput) -> str:
    return f"[{spec.run_id}]({spec.run_url})" if spec.run_url else spec.run_id


async def hold(source: TaskSource, key: str, comment: str) -> None:
    """Hold the task for a person: add `fact:needs-human` (never removing `fact:ready`) and comment why."""
    await source.set_labels(key, add=[NEEDS_HUMAN], remove=[RUNNING])
    await source.comment(key, comment)


async def report(source: TaskSource, spec: ReportStatusInput) -> None:
    """Apply `spec.event` to task `spec.task_key` in `source`."""
    key = spec.task_key
    match spec.event:
        case "run_started":
            await source.set_labels(key, add=[RUNNING], remove=[])
            await source.comment(key, f"fact started run {_run_link(spec)}.")
            await source.advance_status(key, TaskStatus.in_progress)
        case "gate_opened":
            await source.set_labels(key, add=[NEEDS_HUMAN], remove=[])
            await source.comment(key, f"Run {_run_link(spec)} is waiting for you: {spec.detail or 'a gate is open'}.")
        case "gate_answered":
            await source.set_labels(key, add=[], remove=[NEEDS_HUMAN])
        case "prs_opened":
            await source.link_prs(key, spec.prs)
            await source.advance_status(key, TaskStatus.in_review)
        case "merged":
            await source.close(key)
            await source.set_labels(key, add=[], remove=[RUNNING, NEEDS_HUMAN])
        case "stopped" | "needs_human" | "pr_opened" | "closed":
            reason = f": {spec.detail}" if spec.detail else ""
            ended = f"Run {_run_link(spec)} ended {spec.event}{reason}."
            await hold(source, key, f"{ended}\n\n{held_text(key, HELD_ENDS[spec.event])}")
