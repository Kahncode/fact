"""The runs the scheduler counts: each task's run history, and the active runs per project against the caps."""

from collections import defaultdict

from fact.api.run_start import counts_as_run
from fact.core.enums import RunOutcome, RunStatus, RunTrigger
from fact.core.models import Project, Run
from fact.core.scheduler import RunRecord
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

ACTIVE = (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING)


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


async def active_by_project(session: AsyncSession) -> dict[str, int]:
    query = (
        select(Project.name, func.count(Run.id))
        .join(Project, Project.id == Run.project_id)
        .where(Run.status.in_(ACTIVE))
        .group_by(Project.name)
    )
    return {name: int(count) for name, count in (await session.execute(query)).all()}
