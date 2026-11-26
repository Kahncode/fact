"""
The scheduler's contract: the `fact-scheduler` Temporal Schedule, its `SchedulerTick` workflow and activities.

Every tick (30 s by default) asks the API which tasks are ready (`find_ready_tasks`), picks the ones to start with
`plan_starts`, checks the Claude usage gate (`check_claude_usage`, only when something would start) and starts each
through `POST /runs/start` (`start_scheduled_run`). The worker owns the Schedule (`fact.worker.scheduler`); the API and
the CLI pause, unpause and describe it by `SCHEDULE_ID`.

`plan` orders every candidate and says why each one waits, if it does; `plan_starts` is what the tick starts and
`fact scheduler status` previews the same `plan`. Both are pure and deterministic: the tick workflow calls them.
"""

import re
from collections.abc import Sequence
from enum import StrEnum

from fact.core.enums import RunOutcome, RunStatus, RunTrigger
from pydantic import BaseModel, ConfigDict, Field

SCHEDULE_ID = "fact-scheduler"
SCHEDULER_TICK = "SchedulerTick"  # workflow type, on the control queue
TICK_WORKFLOW_ID = "fact-scheduler-tick"  # Temporal appends the scheduled time to each tick's id

FIND_READY_TASKS = "find_ready_tasks"
START_SCHEDULED_RUN = "start_scheduled_run"
CHECK_CLAUDE_USAGE = "check_claude_usage"

_ID_RE = re.compile(r"^M(\d+)-(\d+)$")


class Hold(StrEnum):
    """Why a ready task does not start on this tick."""

    DEPENDENCY = "dependency"  # a dependency is not done
    FAILURE_CAP = "failure_cap"  # its last `concurrency.max_failed_runs` runs failed: a person restarts it
    CAP = "cap"  # `concurrency.max_runs` or the project's `max_runs` is reached
    USAGE = "usage"  # a Claude usage window is inside its reserve


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class ReadyTask(_Model):
    """A task marked ready on its project's default branch with no run in progress and none that ended otherwise."""

    project: str
    task_key: str
    task_id: str
    depth: int = 0  # dependency depth among the project's task files: shallower tasks start first
    waiting_on: list[str] = Field(default_factory=list)  # dependencies not done yet
    failed_runs: int = 0  # failed runs in a row, counted since the last run a person started
    hold: Hold | None = None  # DEPENDENCY or FAILURE_CAP, set by `find_ready_tasks`


class ProjectLoad(_Model):
    project: str
    active: int  # runs queued, running or waiting
    max_runs: int


class ReadyTasks(_Model):
    """What `find_ready_tasks` returns: the candidates, and how many runs are active against each cap."""

    tasks: list[ReadyTask] = Field(default_factory=list)
    projects: list[ProjectLoad] = Field(default_factory=list)
    active: int = 0  # all projects together
    max_runs: int = 2


class PlannedTask(_Model):
    task: ReadyTask
    hold: Hold | None = None  # None: the tick starts it
    detail: str = ""


class StartScheduledRunInput(_Model):
    project: str
    task_key: str


class TickResult(_Model):
    started: list[str] = Field(default_factory=list)  # workflow ids
    failed: list[str] = Field(default_factory=list)  # task keys the API refused or that failed to start
    usage_full: str = ""  # why the usage gate held every start, if it did


class RunRecord(_Model):
    """One counted run of a task, for `run_history_hold`."""

    status: RunStatus
    outcome: RunOutcome | None = None
    trigger: RunTrigger = RunTrigger.MANUAL


def run_history_hold(newest_first: Sequence[RunRecord], max_failed: int) -> tuple[bool, int, Hold | None]:
    """
    `(offered, failed_runs, hold)` for a task with these runs.

    Not offered: a run is in progress, or the last one ended any way but `failed` (a person restarts it). A task whose
    runs all failed is offered again, held by FAILURE_CAP once `max_failed` failed in a row; the count stops at the
    newest run a person started, so `fact run` resets it.
    """
    if any(record.status != RunStatus.FINISHED for record in newest_first):
        return False, 0, None
    failed = 0
    for record in newest_first:
        if record.outcome != RunOutcome.FAILED:
            if failed == 0:
                return False, 0, None
            break
        failed += 1
        if record.trigger == RunTrigger.MANUAL:
            break
    return True, failed, Hold.FAILURE_CAP if failed >= max_failed else None


def _order(task: ReadyTask) -> tuple[int, int, int, str, str]:
    """Depth, then milestone and number for a file task (`M2-09`), else the issue number (`12`), then the id."""
    if m := _ID_RE.match(task.task_id):
        milestone, number = int(m.group(1)), int(m.group(2))
    else:
        milestone, number = 1 << 30, int(task.task_id) if task.task_id.isdigit() else 1 << 30
    return task.depth, milestone, number, task.task_id, task.project


def _held_detail(task: ReadyTask) -> str:
    if task.hold == Hold.DEPENDENCY:
        return "waits on " + ", ".join(task.waiting_on)
    if task.hold == Hold.FAILURE_CAP:
        return f"{task.failed_runs} failed runs in a row: start it with `fact run`"
    return ""


def plan(ready: ReadyTasks, usage_full: str = "") -> list[PlannedTask]:
    """
    Every candidate in start order (dependency depth, then id) with the reason it waits, if any.

    The overall and per-project caps apply in that order; `usage_full` (why the usage gate holds) holds every task the
    caps would let start.
    """
    free = ready.max_runs - ready.active
    per_project = {p.project: p.max_runs - p.active for p in ready.projects}
    planned: list[PlannedTask] = []
    for task in sorted(ready.tasks, key=_order):
        if task.hold is not None:
            planned.append(PlannedTask(task=task, hold=task.hold, detail=_held_detail(task)))
        elif free <= 0:
            planned.append(PlannedTask(task=task, hold=Hold.CAP, detail=f"max_runs {ready.max_runs} reached"))
        elif per_project.get(task.project, 0) <= 0:
            planned.append(PlannedTask(task=task, hold=Hold.CAP, detail=f"project {task.project} max_runs reached"))
        else:
            free -= 1
            per_project[task.project] -= 1
            hold = Hold.USAGE if usage_full else None
            planned.append(PlannedTask(task=task, hold=hold, detail=usage_full))
    return planned


def plan_starts(ready: ReadyTasks) -> list[ReadyTask]:
    """Tasks the caps let start now, in order; the usage gate is checked after (only when this is not empty)."""
    return [p.task for p in plan(ready) if p.hold is None]
