"""
The scheduler's contract: the `fact-scheduler` Temporal Schedule, its `SchedulerTick` workflow and activities.

Every tick (30 s by default) asks the API which tasks are ready (`find_ready_tasks`), picks the ones to start with
`plan_starts`, and starts each through `POST /runs/start` (`start_scheduled_run`). The worker owns the Schedule
(`fact.worker.scheduler`); the API and the CLI pause, unpause and describe it by `SCHEDULE_ID`.

`plan_starts` is pure and deterministic: the tick workflow calls it.
"""

import re

from pydantic import BaseModel, ConfigDict, Field

SCHEDULE_ID = "fact-scheduler"
SCHEDULER_TICK = "SchedulerTick"  # workflow type, on the control queue
TICK_WORKFLOW_ID = "fact-scheduler-tick"  # Temporal appends the scheduled time to each tick's id

FIND_READY_TASKS = "find_ready_tasks"
START_SCHEDULED_RUN = "start_scheduled_run"

_ID_RE = re.compile(r"^M(\d+)-(\d+)$")


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class ReadyTask(_Model):
    """A task the scheduler may start: `ready` on the default branch, every dependency done, never run."""

    project: str
    task_key: str
    task_id: str
    depth: int = 0  # dependency depth among the project's task files: shallower tasks start first


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


class StartScheduledRunInput(_Model):
    project: str
    task_key: str


class TickResult(_Model):
    started: list[str] = Field(default_factory=list)  # workflow ids
    failed: list[str] = Field(default_factory=list)  # task keys the API refused or that failed to start


def _order(task: ReadyTask) -> tuple[int, int, int, str, str]:
    m = _ID_RE.match(task.task_id)
    milestone, number = (int(m.group(1)), int(m.group(2))) if m else (1 << 30, 1 << 30)
    return task.depth, milestone, number, task.task_id, task.project


def plan_starts(ready: ReadyTasks) -> list[ReadyTask]:
    """Tasks to start now: by dependency depth, then id, while the overall and per-project caps allow."""
    free = ready.max_runs - ready.active
    per_project = {p.project: p.max_runs - p.active for p in ready.projects}
    chosen: list[ReadyTask] = []
    for task in sorted(ready.tasks, key=_order):
        if free <= 0:
            break
        if per_project.get(task.project, 0) <= 0:
            continue
        chosen.append(task)
        free -= 1
        per_project[task.project] -= 1
    return chosen
