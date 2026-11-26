"""
What the scheduler may start: ready tasks on each project's default branch, and the runs counted against the caps.

A task with a run in progress is not offered. A task whose last run ended any way but `failed` (needs a person,
dropped, PR open or closed) is not offered again: a person restarts it with `fact run`. A task whose runs failed is
offered again until `concurrency.max_failed_runs` failed in a row (counted from the last run a person started), then
held with `Hold.FAILURE_CAP` (`run_history_hold`). A start that never reached Temporal (`finished` and never started)
does not count. Tasks whose dependencies are not done are listed held with `Hold.DEPENDENCY`, for the preview.

`preview` is `fact scheduler status`: `plan` over the same `find_ready`, with the usage gate's last answer.
"""

import asyncio
from collections import defaultdict
from datetime import UTC, datetime

from fact.api.run_start import counts_as_run
from fact.api.task_ref import TaskReadError, project_tasks
from fact.core.enums import RunOutcome, RunStatus, RunTrigger
from fact.core.logging import get_logger
from fact.core.models import Project, Run
from fact.core.project_config import ConfigError
from fact.core.scheduler import (
    Hold,
    PlannedTask,
    ProjectLoad,
    ReadyTask,
    ReadyTasks,
    RunRecord,
    plan,
    run_history_hold,
)
from fact.core.settings import ProjectRef, Settings
from fact.core.usage_gate import UsageCheck
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

log = get_logger(__name__)
ACTIVE = (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING)


class SchedulerPreview(BaseModel):
    tasks: list[PlannedTask] = Field(default_factory=list)  # in the order the next tick would start them
    usage: UsageCheck | None = None  # the usage gate's last answer; None before the first since the API started
    active: int = 0
    max_runs: int = 0


async def _ready_in(settings: Settings, ref: ProjectRef) -> list[ReadyTask]:
    """Read the project's ready tasks from its task source (`files`: on `origin/<default>`); none without one."""
    try:
        async with project_tasks(settings, ref) as tasks:
            found = await tasks.source.list_ready(include_waiting=True) if tasks else []
    except (ConfigError, TaskReadError) as exc:
        log.warning("scheduler_skipped_project", project=ref.name, error=str(exc))
        return []
    return [
        ReadyTask(
            project=ref.name,
            task_key=r.task.key,
            task_id=r.task.id,
            depth=r.depth,
            waiting_on=list(r.waiting_on),
            hold=Hold.DEPENDENCY if r.waiting_on else None,
        )
        for r in found
    ]


def _record(status: str, outcome: str | None, trigger: str) -> RunRecord:
    return RunRecord(
        status=RunStatus(status), outcome=RunOutcome(outcome) if outcome else None, trigger=RunTrigger(trigger)
    )


async def run_histories(
    session: AsyncSession, project: str | None = None, task_key: str | None = None
) -> dict[tuple[str, str], list[RunRecord]]:
    """Read the counted runs of every task (or one), newest first, by (project, task key)."""
    query = (
        select(Project.name, Run.task_key, Run.status, Run.outcome, Run.trigger)
        .join(Project, Project.id == Run.project_id)
        .where(counts_as_run())
        .order_by(Run.created_at.desc(), Run.id.desc())
    )
    if project is not None and task_key is not None:
        query = query.where(Project.name == project, Run.task_key == task_key)
    histories: dict[tuple[str, str], list[RunRecord]] = defaultdict(list)
    for name, key, status, outcome, trigger in (await session.execute(query)).all():
        histories[name, key].append(_record(status, outcome, trigger))
    return histories


def apply_history(task: ReadyTask, history: list[RunRecord], max_failed: int) -> ReadyTask | None:
    """Return the task as the scheduler sees it after its runs; None when it is not offered."""
    offered, failed, hold = run_history_hold(history, max_failed)
    if not offered:
        return None
    return task.model_copy(update={"failed_runs": failed, "hold": task.hold or hold})


async def active_by_project(session: AsyncSession) -> dict[str, int]:
    query = (
        select(Project.name, func.count(Run.id))
        .join(Project, Project.id == Run.project_id)
        .where(Run.status.in_(ACTIVE))
        .group_by(Project.name)
    )
    return {name: int(count) for name, count in (await session.execute(query)).all()}


async def find_ready(settings: Settings, session: AsyncSession) -> ReadyTasks:
    """Every project's offered tasks, and the active runs against `concurrency.max_runs` and each project's cap."""
    # one slow remote delays no other
    found = await asyncio.gather(*(_ready_in(settings, ref) for ref in settings.projects))
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
