"""
What each run event does to the task in its source: labels, comments, PR links and closing.

The `report_status` activity calls `report`; a source that keeps no state of its own (files) only logs each write.
"""

from fact.core.activity_contract import ReportStatusInput
from fact.core.tasks.base import TaskSource

RUNNING = "fact:running"
NEEDS_HUMAN = "fact:needs-human"


def _run_link(spec: ReportStatusInput) -> str:
    return f"[{spec.run_id}]({spec.run_url})" if spec.run_url else spec.run_id


async def report(source: TaskSource, spec: ReportStatusInput) -> None:
    """Apply `spec.event` to task `spec.task_key` in `source`."""
    key = spec.task_key
    match spec.event:
        case "run_started":
            await source.set_labels(key, add=[RUNNING], remove=[])
            await source.comment(key, f"fact started run {_run_link(spec)}.")
        case "gate_opened":
            await source.set_labels(key, add=[NEEDS_HUMAN], remove=[])
            await source.comment(key, f"Run {_run_link(spec)} is waiting for you: {spec.detail or 'a gate is open'}.")
        case "gate_answered":
            await source.set_labels(key, add=[], remove=[NEEDS_HUMAN])
        case "prs_opened":
            await source.link_prs(key, spec.prs)
        case "merged":
            await source.close(key)
            await source.set_labels(key, add=[], remove=[RUNNING, NEEDS_HUMAN])
        case "closed" | "stopped":
            await source.set_labels(key, add=[], remove=[RUNNING, NEEDS_HUMAN])
            reason = f": {spec.detail}" if spec.detail else ""
            await source.comment(key, f"Run {_run_link(spec)} ended {spec.event}{reason}.")
