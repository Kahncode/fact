"""
What the scheduler may start: ready tasks on each project's default branch, and the runs counted against the caps.

A task with a run in progress is not offered. A task whose last run ended any way but `failed` (needs a person,
stopped, PR open or closed) is not offered again: a person restarts it with `fact run`. A task whose runs failed is
offered again until `concurrency.max_failed_runs` failed in a row (counted from the last run a person started), then
held with `Hold.FAILURE_CAP` (`run_history_hold`). A start that never reached Temporal (`finished` and never started)
does not count. Tasks under an ancestor not marked ready are listed held with `Hold.PARENT`, and tasks whose
dependencies are not done with `Hold.DEPENDENCY`, for the preview. Only the tick's read (`close_epics`) closes the
ready epics with nothing left to wait on; the preview never writes.

`run_tick` is the scheduler's tick, in this process: `find_ready`, `plan_starts`, the usage gate only when something
is chosen, then each start through `start_task` (`POST /runs/start` with `scheduled: true`, called directly). It
touches Temporal only to start a run. `SchedulerRuntime.lock` keeps two ticks of this process from overlapping: a tick
that finds it held returns at once. Callers: `POST /scheduler/tick` everywhere, and the dev loop
(`fact.api.scheduler_loop`).

`preview` is `fact scheduler status`: `plan` over the same `find_ready`, with the usage gate's last answer.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fact.api.routers.run_start import RunStart, start_task
from fact.api.run_counts import active_by_project, run_histories
from fact.api.task_ref import TaskReadError, project_tasks
from fact.api.usage_gate import UsageGate
from fact.core.logging import get_logger
from fact.core.project_config import ConfigError
from fact.core.scheduler import (
    Hold,
    PlannedTask,
    ProjectLoad,
    ReadyTask,
    ReadyTasks,
    RunRecord,
    TickResult,
    effective_priorities,
    plan,
    plan_starts,
    run_history_hold,
)
from fact.core.settings import ProjectRef, Settings
from fact.core.tasks import ReadyItem
from fact.core.usage_gate import GateState, UsageCheck
from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import State

log = get_logger(__name__)


@dataclass
class SchedulerRuntime:
    """The scheduler's state in the API process (`app.state.scheduler`); losing it on a restart changes nothing else."""

    gate: UsageGate
    on: bool = True  # dev only: whether the loop ticks (`fact scheduler on|off`); on at every API start
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_tick_at: datetime | None = None
    last_tick: TickResult | None = None


class SchedulerPreview(BaseModel):
    tasks: list[PlannedTask] = Field(default_factory=list)  # in the order the next tick would start them
    usage: UsageCheck | None = None  # the usage gate's last answer; None before the first since the API started
    active: int = 0
    max_runs: int = 0


def _hold(item: ReadyItem) -> Hold | None:
    if item.gated_by is not None:
        return Hold.PARENT
    return Hold.DEPENDENCY if item.waiting_on else None


async def _ready_in(settings: Settings, ref: ProjectRef, *, close_epics: bool = False) -> list[ReadyTask]:
    """
    Read the project's ready tasks from its task source (`files`: on `origin/<default>`); none without one.

    A task's effective priority is raised by the project's listed tasks that wait on it (`effective_priorities`).
    """
    try:
        async with project_tasks(settings, ref) as tasks:
            found = await tasks.source.list_ready(include_waiting=True, close_epics=close_epics) if tasks else []
    except (ConfigError, TaskReadError) as exc:
        log.warning("scheduler_skipped_project", project=ref.name, error=str(exc))
        return []
    ready = [
        ReadyTask(
            project=ref.name,
            task_key=r.task.key,
            task_id=r.task.id,
            priority=r.task.priority,
            depth=r.depth,
            waiting_on=list(r.waiting_on),
            gated_by=r.gated_by,
            hold=_hold(r),
        )
        for r in found
    ]
    return effective_priorities(ready)


def apply_history(task: ReadyTask, history: list[RunRecord], max_failed: int) -> ReadyTask | None:
    """Return the task as the scheduler sees it after its runs; None when it is not offered."""
    offered, failed, hold = run_history_hold(history, max_failed)
    if not offered:
        return None
    return task.model_copy(update={"failed_runs": failed, "hold": task.hold or hold})


async def find_ready(settings: Settings, session: AsyncSession, *, close_epics: bool = False) -> ReadyTasks:
    """
    Every project's offered tasks, and the active runs against `concurrency.max_runs` and each project's cap.

    `close_epics`: the scheduler's tick, which also closes the ready epics with nothing left to wait on.
    """
    # one slow remote delays no other
    found = await asyncio.gather(*(_ready_in(settings, ref, close_epics=close_epics) for ref in settings.projects))
    histories = await run_histories(session)
    max_failed = settings.concurrency.max_failed_runs
    offered = (apply_history(t, histories.get((t.project, t.task_key), []), max_failed) for ts in found for t in ts)
    active = await active_by_project(session)
    return ReadyTasks(
        tasks=[t for t in offered if t is not None],
        projects=[
            ProjectLoad(project=r.name, active=active.get(r.name, 0), max_runs=r.max_runs) for r in settings.projects
        ],
        active=sum(active.values()),
        max_runs=settings.concurrency.max_runs,
    )


def preview(ready: ReadyTasks, usage: UsageCheck | None, now: datetime | None = None) -> SchedulerPreview:
    """Say what the next tick would do with `ready`, judged on the usage gate's last answer (never a new call)."""
    held = usage is not None and usage.held(now or datetime.now(UTC))
    return SchedulerPreview(
        tasks=plan(ready, usage.reason if held and usage else ""),
        usage=usage,
        active=ready.active,
        max_runs=ready.max_runs,
    )


async def run_tick(state: State) -> TickResult:
    """Run one tick, unless another is running in this process: then return at once, `skipped`."""
    runtime: SchedulerRuntime = state.scheduler
    if runtime.lock.locked():
        return TickResult(skipped=True)
    async with runtime.lock:
        result = await _tick(state, runtime.gate)
        runtime.last_tick_at, runtime.last_tick = datetime.now(UTC), result
        return result


async def _tick(state: State, gate: UsageGate) -> TickResult:
    async with state.sessions() as session:
        ready = await find_ready(state.settings, session, close_epics=True)  # the tick: closes finished epics
    chosen = plan_starts(ready)
    if not chosen:
        return TickResult()
    if reason := await _usage_full(gate):
        return TickResult(usage_full=reason)
    started: list[str] = []
    failed: list[str] = []
    for task in chosen:
        body = RunStart(task_key=task.task_key, project=task.project, scheduled=True)
        try:
            async with state.sessions() as session:
                run = await start_task(state, body, session)
        except HTTPException as exc:  # refused (the task changed or a cap filled since the read), or Temporal down
            log.warning("scheduled_start_refused", task_key=task.task_key, status=exc.status_code, detail=exc.detail)
            failed.append(task.task_key)
            continue
        except Exception:
            log.exception("scheduled_start_failed", task_key=task.task_key, project=task.project)
            failed.append(task.task_key)
            continue
        log.info("scheduled_run_started", task_key=task.task_key, project=task.project, workflow_id=run.workflow_id)
        started.append(run.workflow_id)
    return TickResult(started=started, failed=failed)


async def _usage_full(gate: UsageGate) -> str:
    """Why a full usage window holds every start; "" to start (unreadable usage is an error, not a block)."""
    try:
        check = await gate.check(datetime.now(UTC))
    except Exception:
        log.exception("usage_gate_failed")
        return ""
    if check.state == GateState.UNAVAILABLE:
        log.error("claude_usage_unavailable", error=check.error)
    elif check.state == GateState.FULL:
        log.info("claude_usage_full", windows=check.reason, next_call_at=check.next_call_at)
        return check.reason
    return ""
