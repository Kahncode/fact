"""
What the scheduler may start: ready tasks on each project's default branch, and the runs counted against the caps.

A task that has had a run, whatever the outcome, is never offered again: a run that ended without merging (failed,
needs a person, dropped, PR closed) is restarted by a person with `fact run`. A start that never reached Temporal
(`finished` and never started) does not count.
"""

import asyncio

from fact.api.projects import local_path
from fact.api.run_start import counts_as_run, default_branch
from fact.api.task_ref import TaskReadError, fetch, ready_tasks
from fact.core.enums import RunStatus
from fact.core.logging import get_logger
from fact.core.models import Project, Run
from fact.core.project_config import ConfigError, load_project_config
from fact.core.scheduler import ProjectLoad, ReadyTask, ReadyTasks
from fact.core.settings import ProjectRef, Settings
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

log = get_logger(__name__)
ACTIVE = (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING)


def _read_ready(ref: ProjectRef) -> list[ReadyTask]:
    """Fetch, then read the project's ready tasks from `origin/<default>`; none without a local files source."""
    path = local_path(ref)
    if path is None:
        return []
    config = load_project_config(path)
    if config.task_source.kind != "files":
        return []
    branch = default_branch(ref, config)
    fetch(path, branch)
    found = ready_tasks(path, branch, config.task_source.path)
    return [ReadyTask(project=ref.name, task_key=t.key, task_id=t.id, depth=depth) for t, depth in found]


async def _ready_in(ref: ProjectRef) -> list[ReadyTask]:
    try:
        return await asyncio.to_thread(_read_ready, ref)  # git is blocking
    except (ConfigError, TaskReadError) as exc:
        log.warning("scheduler_skipped_project", project=ref.name, error=str(exc))
        return []


async def _run_tasks(session: AsyncSession) -> set[tuple[str, str]]:
    """(project, task key) of every task with a run, active or ended; a start that never reached Temporal is not one."""
    query = (
        select(Project.name, Run.task_key).join(Project, Project.id == Run.project_id).where(counts_as_run()).distinct()
    )
    return {(name, key) for name, key in (await session.execute(query)).all()}


async def _active_by_project(session: AsyncSession) -> dict[str, int]:
    query = (
        select(Project.name, func.count(Run.id))
        .join(Project, Project.id == Run.project_id)
        .where(Run.status.in_(ACTIVE))
        .group_by(Project.name)
    )
    return {name: int(count) for name, count in (await session.execute(query)).all()}


async def find_ready(settings: Settings, session: AsyncSession) -> ReadyTasks:
    """Every project's ready tasks that never ran, and the active runs against `concurrency.max_runs` and each cap."""
    found = await asyncio.gather(*(_ready_in(ref) for ref in settings.projects))  # one slow remote delays no other
    tasks = [t for project_tasks in found for t in project_tasks]
    ran = await _run_tasks(session)
    active = await _active_by_project(session)
    return ReadyTasks(
        tasks=[t for t in tasks if (t.project, t.task_key) not in ran],
        projects=[
            ProjectLoad(project=r.name, active=active.get(r.name, 0), max_runs=r.max_runs) for r in settings.projects
        ],
        active=sum(active.values()),
        max_runs=settings.concurrency.max_runs,
    )
