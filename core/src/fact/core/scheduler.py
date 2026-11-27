"""
The scheduler's contract: what a tick reads, plans and returns.

A tick (`fact.api.scheduler.run_tick`, in the API process) reads which tasks are ready (`find_ready`), picks the
ones to start with `plan_starts`, checks the Claude usage gate only when something would start, and starts each
through the same code as `POST /runs/start` with `scheduled: true`. No Temporal workflow or activity is involved:
Temporal runs only project workflows. `SCHEDULE_ID` names the Temporal Schedule that ran the tick before; the worker
deletes it at start when a dev Temporal database still has it.

`plan` orders every candidate and says why each one waits, if it does; `plan_starts` is what the tick starts and
`fact scheduler status` previews the same `plan`. Both are pure and deterministic. Each project's candidates are
ordered on their own, by effective priority (`effective_priorities`), then dependency depth, then id; the projects
take turns (in name order) for the shared `concurrency.max_runs`, each offering its next task.
"""

import re
from collections import deque
from collections.abc import Sequence
from enum import StrEnum

from fact.core.enums import RunOutcome, RunStatus, RunTrigger
from fact.core.holds import NEEDS_HUMAN, release_hint
from fact.core.types import PRIORITIES, Priority
from pydantic import BaseModel, ConfigDict, Field

SCHEDULE_ID = "fact-scheduler"  # the retired Temporal Schedule, deleted by the worker at start

_ID_RE = re.compile(r"^M(\d+)-(\d+)$")


class Hold(StrEnum):
    """Why a ready task does not start on this tick."""

    NEEDS_HUMAN = "needs_human"  # an issue carrying `fact:needs-human`: a person removes it or runs `fact run`
    DEPENDENCY = "dependency"  # a dependency is not done
    PARENT = "parent"  # an ancestor (parent issue) is not marked ready: its label gates the task
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
    priority: Priority = "medium"
    effective_priority: Priority = "medium"  # the highest of its own and that of every ready task waiting on it
    depth: int = 0  # dependency depth among the project's task files: shallower tasks start first
    waiting_on: list[str] = Field(default_factory=list)  # dependencies not done yet
    gated_by: str | None = None  # the nearest ancestor not marked ready
    failed_runs: int = 0  # failed runs in a row, counted since the last run a person started
    last_run_id: str | None = None  # its newest counted run, if any
    hold: Hold | None = None  # NEEDS_HUMAN, PARENT, DEPENDENCY or FAILURE_CAP, set by `find_ready`


class ProjectLoad(_Model):
    project: str
    active: int  # runs queued, running or waiting
    max_runs: int


class ReadyTasks(_Model):
    """What `find_ready` returns: the candidates, and how many runs are active against each cap."""

    tasks: list[ReadyTask] = Field(default_factory=list)
    projects: list[ProjectLoad] = Field(default_factory=list)
    active: int = 0  # all projects together
    max_runs: int = 2


class PlannedTask(_Model):
    task: ReadyTask
    hold: Hold | None = None  # None: the tick starts it
    detail: str = ""


class TickResult(_Model):
    started: list[str] = Field(default_factory=list)  # workflow ids
    failed: list[str] = Field(default_factory=list)  # task keys the API refused or that failed to start
    usage_full: str = ""  # why the usage gate held every start, if it did
    skipped: bool = False  # another tick was still running in this API process: this one did nothing


class RunRecord(_Model):
    """One counted run of a task, for `run_history_hold`."""

    status: RunStatus
    outcome: RunOutcome | None = None
    trigger: RunTrigger = RunTrigger.MANUAL
    run_id: str = ""


def run_history_hold(
    newest_first: Sequence[RunRecord], max_failed: int, *, labelled: bool = False
) -> tuple[bool, int, Hold | None]:
    """
    `(offered, failed_runs, hold)` for a task with these runs.

    Not offered: a run is in progress. A file task (`labelled` False) whose last run ended any way but `failed` is not
    offered either (a person restarts it). An issue (`labelled`) is held by its `fact:needs-human` label instead, which
    such run ends add, so its history holds nothing but the failure count. A task whose last runs failed is offered
    again, held by FAILURE_CAP once `max_failed` failed in a row; the count stops at the newest run that did not fail
    and at the newest run a person started, so `fact run` resets it.
    """
    if any(record.status != RunStatus.FINISHED for record in newest_first):
        return False, 0, None
    failed = 0
    for record in newest_first:
        if record.outcome != RunOutcome.FAILED:
            if failed == 0 and not labelled:
                return False, 0, None
            break
        failed += 1
        if record.trigger == RunTrigger.MANUAL:
            break
    return True, failed, Hold.FAILURE_CAP if failed >= max_failed else None


def effective_priorities(tasks: Sequence[ReadyTask]) -> list[ReadyTask]:
    """
    Set each task's `effective_priority`: the highest of its own and that of every task here waiting on it.

    Waits are followed transitively through `waiting_on`, within a project only: a wait on a task of another project,
    or on one not among `tasks`, raises nothing.
    """
    by_key = {(t.project, t.task_key): t for t in tasks}
    rank = {key: PRIORITIES.index(t.priority) for key, t in by_key.items()}
    for waiter in tasks:
        own, seen = PRIORITIES.index(waiter.priority), {waiter.task_key}
        stack = list(waiter.waiting_on)
        while stack:
            key = stack.pop()
            if key in seen or (waiter.project, key) not in by_key:
                continue
            seen.add(key)
            rank[waiter.project, key] = min(rank[waiter.project, key], own)
            stack.extend(by_key[waiter.project, key].waiting_on)
    return [t.model_copy(update={"effective_priority": PRIORITIES[rank[t.project, t.task_key]]}) for t in tasks]


def _order(task: ReadyTask) -> tuple[int, int, int, int, str]:
    """Effective priority, depth, then milestone and number for a file task (`M2-09`) else the issue number, then id."""
    if m := _ID_RE.match(task.task_id):
        milestone, number = int(m.group(1)), int(m.group(2))
    else:
        milestone, number = 1 << 30, int(task.task_id) if task.task_id.isdigit() else 1 << 30
    return PRIORITIES.index(task.effective_priority), task.depth, milestone, number, task.task_id


def _held_detail(task: ReadyTask) -> str:
    if task.hold == Hold.NEEDS_HUMAN:
        return f"carries `{NEEDS_HUMAN}`: {release_hint(task.task_key)}"
    if task.hold == Hold.PARENT:
        return f"waits for its ancestor {task.gated_by} to be marked ready"
    if task.hold == Hold.DEPENDENCY:
        return "waits on " + ", ".join(task.waiting_on)
    if task.hold == Hold.FAILURE_CAP:
        return f"{task.failed_runs} failed runs in a row: start it with `fact run`"
    return ""


class _Slots:
    """The free slots under `concurrency.max_runs` and each project's `max_runs`, taken as `plan` gives them out."""

    def __init__(self, ready: ReadyTasks, usage_full: str) -> None:
        self.max_runs = ready.max_runs
        self.free = ready.max_runs - ready.active
        self.per_project = {p.project: p.max_runs - p.active for p in ready.projects}
        self.usage_full = usage_full

    def take(self, task: ReadyTask) -> PlannedTask:
        if self.free <= 0:
            return PlannedTask(task=task, hold=Hold.CAP, detail=f"max_runs {self.max_runs} reached")
        if self.per_project.get(task.project, 0) <= 0:
            return PlannedTask(task=task, hold=Hold.CAP, detail=f"project {task.project} max_runs reached")
        self.free -= 1
        self.per_project[task.project] -= 1
        return PlannedTask(task=task, hold=Hold.USAGE if self.usage_full else None, detail=self.usage_full)


def _offer_next(queue: deque[ReadyTask], slots: _Slots, planned: list[PlannedTask]) -> None:
    """One project's turn: list its held tasks up to its next candidate, and give that one a slot if one is free."""
    while queue:
        task = queue.popleft()
        if task.hold is None:
            planned.append(slots.take(task))
            return
        planned.append(PlannedTask(task=task, hold=task.hold, detail=_held_detail(task)))


def plan(ready: ReadyTasks, usage_full: str = "") -> list[PlannedTask]:
    """
    Every candidate in start order with the reason it waits, if any.

    Each project's tasks are ordered on their own by effective priority, then dependency depth, then id. The projects
    take turns in name order, each offering its next task, so one project's priorities never take a slot from
    another. The overall and per-project caps apply in that order; `usage_full` (why the usage gate holds) holds every
    task the caps would let start.
    """
    queues: dict[str, deque[ReadyTask]] = {}
    for task in sorted(ready.tasks, key=lambda t: (t.project, _order(t))):
        queues.setdefault(task.project, deque()).append(task)
    slots = _Slots(ready, usage_full)
    planned: list[PlannedTask] = []
    while queues:
        for project, queue in list(queues.items()):
            _offer_next(queue, slots, planned)
            if not queue:
                del queues[project]
    return planned


def plan_starts(ready: ReadyTasks) -> list[ReadyTask]:
    """Tasks the caps let start now, in order; the usage gate is checked after (only when this is not empty)."""
    return [p.task for p in plan(ready) if p.hold is None]
