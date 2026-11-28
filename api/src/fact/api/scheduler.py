"""
What the scheduler may start: ready tasks on each project's default branch, and the runs counted against the caps.

A task with a run in progress is not offered. An issue carrying `fact:needs-human` is listed held with
`Hold.NEEDS_HUMAN`: the run ends the scheduler does not retry (needs a person, stopped, PR open or closed) add that
label, so an issue's run history holds nothing else. A file task, which has no labels, whose last run ended any way but
`failed` is not offered again: a person restarts it with `fact run`. A task whose runs failed is offered again until
`concurrency.max_failed_runs` failed in a row (counted from the last run a person started), then held with
`Hold.FAILURE_CAP` (`run_history_hold`); the tick then labels the issue (`fact.api.failure_cap`). A start that never
reached Temporal (`finished` and never started) does not count. Tasks under an ancestor not marked ready are listed
held with `Hold.PARENT`, and tasks whose dependencies are not done with `Hold.DEPENDENCY`, for the preview. Only the
tick's read (`close_parents`) closes the ready parents without a work type with nothing left to wait on and labels
failure-capped issues; the preview never writes. A failure-capped issue whose label a person removed is released (`failure_cap.released`).

Tasks come from each project's kept listing (`fact.api.listings`), listed again once empty, older than
`concurrency.listing_max_age_seconds` or missing an urgent ready task (one filtered read per tick, GitHub only), and
not at all while the project's pool is full; run history is read at every tick, and every start reads its task again,
so an old listing never starts a closed or taken task.

`run_tick` is the scheduler's tick, in this process: `find_ready`, `plan_starts`, the usage gate only when something
is chosen, then each start through `start_task` (`POST /runs/start` with `scheduled: true`, called directly). It
touches Temporal only to start a run. `SchedulerRuntime.lock` keeps two ticks of this process from overlapping: a tick
that finds it held returns at once. Callers: `POST /scheduler/tick` everywhere, and the dev loop
(`fact.api.scheduler_loop`).

`preview` is `fact scheduler status`: `plan` over the same kept listings, with the usage gate's last answer.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fact.api.failure_cap import hold_failure_capped, released
from fact.api.listings import Listing, Listings
from fact.api.routers.run_start import RunStart, start_task
from fact.api.run_counts import active_by_project, run_histories
from fact.api.task_ref import TaskReadError, project_tasks, urgent_ready
from fact.api.usage_gate import UsageGate
from fact.core.logging import get_logger
from fact.core.project_config import DEFAULT_MAX_CONCURRENT_RUNS, ConfigError
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
from fact.core.tasks import ReadyItem, is_issue
from fact.core.usage_gate import GateState, UsageCheck
from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import State

log = get_logger(__name__)


@dataclass
class SchedulerRuntime:
    """
    The scheduler's state in the API process (`app.state.scheduler`); losing it on a restart changes nothing else.

    The dev loop's on/off switch is not here: it lives in the database (`fact.api.scheduler_switch`).
    """

    gate: UsageGate
    listings: Listings
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_tick_at: datetime | None = None
    last_tick: TickResult | None = None


class SchedulerPreview(BaseModel):
    tasks: list[PlannedTask] = Field(default_factory=list)  # in the order the next tick would start them
    usage: UsageCheck | None = None  # the usage gate's last answer; None before the first since the API started
    projects: list[ProjectLoad] = Field(default_factory=list)  # each project's active runs against its pool's cap


def _hold(item: ReadyItem) -> Hold | None:
    if item.task.needs_human:
        return Hold.NEEDS_HUMAN
    if item.gated_by is not None:
        return Hold.PARENT
    return Hold.DEPENDENCY if item.waiting_on else None


async def _ready_in(settings: Settings, ref: ProjectRef, *, close_parents: bool = False) -> tuple[list[ReadyTask], int]:
    """
    Read the project's ready tasks from its task source (`files`: on `origin/<default>`), and its pool's cap.

    A project without a checkout or with a broken config or task source offers no task (and keeps the default cap).
    A task's effective priority is raised by the project's listed tasks that wait on it (`effective_priorities`).
    """
    cap = DEFAULT_MAX_CONCURRENT_RUNS
    try:
        async with project_tasks(settings, ref) as tasks:
            if tasks is None:
                return [], cap
            cap = tasks.config.runner_pool.max_concurrent_runs
            found = await tasks.source.list_ready(include_waiting=True, close_parents=close_parents)
    except (ConfigError, TaskReadError) as exc:
        log.warning("scheduler_skipped_project", project=ref.name, error=str(exc))
        return [], cap
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
    return effective_priorities(ready), cap


def apply_history(task: ReadyTask, history: list[RunRecord], max_failed: int) -> ReadyTask | None:
    """Return the task as the scheduler sees it after its runs; None when it is not offered."""
    offered, failed, hold = run_history_hold(history, max_failed, labelled=is_issue(task.task_key))
    if not offered:
        return None
    last = history[0].run_id if history else None
    return task.model_copy(update={"failed_runs": failed, "last_run_id": last, "hold": task.hold or hold})


async def _listing(settings: Settings, listings: Listings, ref: ProjectRef, active: int, *, tick: bool) -> Listing:
    """
    Return the listing to plan from: the kept one, unless the tick should list the project again.

    The tick lists again (closing finished parents) when its pool has room and the kept listing is empty, older than
    `concurrency.listing_max_age_seconds`, or missing an urgent ready task (`_urgent_unlisted`). The preview plans from
    any kept listing, however old, and lists only a project never listed.
    """
    kept = listings.get(ref.name)
    if kept and (not tick or active >= kept.max_concurrent_runs):
        return kept
    if kept and kept.tasks and listings.fresh(ref.name) and not await _urgent_unlisted(settings, ref, kept):
        return kept
    tasks, cap = await _ready_in(settings, ref, close_parents=tick)
    return listings.keep(ref.name, tasks, cap)


async def _urgent_unlisted(settings: Settings, ref: ProjectRef, kept: Listing) -> bool:
    """Whether the source shows a `priority:urgent` ready task the kept listing lacks, from one filtered read."""
    try:
        urgent = await urgent_ready(settings, ref) or []
    except (ConfigError, TaskReadError) as exc:
        log.warning("scheduler_urgent_poll_failed", project=ref.name, error=str(exc))
        return False
    listed = {t.task_key.lower() for t in kept.tasks}
    return any(key.lower() not in listed for key in urgent)


async def find_ready(
    settings: Settings, session: AsyncSession, listings: Listings, *, tick: bool = False
) -> ReadyTasks:
    """
    Every project's offered tasks, and its active runs against its runner pool's `max_concurrent_runs`.

    Tasks come from the kept listings (`_listing`); run history and active runs are read now. `tick`: the scheduler's
    tick, whose new listings also close the ready parents without a work type with nothing left to wait on.
    """
    active = await active_by_project(session)
    # one slow remote delays no other
    found = await asyncio.gather(
        *(_listing(settings, listings, ref, active.get(ref.name, 0), tick=tick) for ref in settings.projects)
    )
    histories = await run_histories(session)
    max_failed = settings.concurrency.max_failed_runs
    offered = (apply_history(t, histories.get((t.project, t.task_key), []), max_failed) for f in found for t in f.tasks)
    tasks = await released(session, [t for t in offered if t is not None])
    return ReadyTasks(
        tasks=tasks,
        projects=[
            ProjectLoad(
                project=r.name,
                active=active.get(r.name, 0),
                max_concurrent_runs=f.max_concurrent_runs,
                listed_at=f.listed_at,
            )
            for r, f in zip(settings.projects, found, strict=True)
        ],
    )


def preview(ready: ReadyTasks, usage: UsageCheck | None, now: datetime | None = None) -> SchedulerPreview:
    """Say what the next tick would do with `ready`, judged on the usage gate's last answer (never a new call)."""
    held = usage is not None and usage.held(now or datetime.now(UTC))
    return SchedulerPreview(
        tasks=plan(ready, usage.reason if held and usage else ""),
        usage=usage,
        projects=ready.projects,
    )


async def run_tick(state: State) -> TickResult:
    """Run one tick, unless another is running in this process: then return at once, `skipped`."""
    runtime: SchedulerRuntime = state.scheduler
    if runtime.lock.locked():
        return TickResult(skipped=True)
    async with runtime.lock:
        result = await _tick(state, runtime)
        runtime.last_tick_at, runtime.last_tick = datetime.now(UTC), result
        return result


async def _tick(state: State, runtime: SchedulerRuntime) -> TickResult:
    gate = runtime.gate
    async with state.sessions() as session:
        ready = await find_ready(state.settings, session, runtime.listings, tick=True)
        await hold_failure_capped(state.settings, session, ready.tasks)
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
            runtime.listings.drop(task.project)  # the listing was wrong about it: list the project again next tick
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
