"""
The scheduler's tick holds an issue that reached `concurrency.max_failed_runs` failed runs in a row.

It adds `fact:needs-human`, comments why and how to release it (`fact.core.tasks.events.hold`) and records one
`task_held` notification on the last failed run, keyed `<run id>:held`. That notification is the record that the
streak was held: a later tick finds it and does nothing, so the hold is written once per streak, whichever API
process ticks. Once the label is gone again, a person removed it: the streak is released (`released`) and the
scheduler retries the issue, until its next failure holds it again. Best effort: a failure is logged and the next tick
tries again.
"""

from collections.abc import Iterable, Sequence

import httpx
from fact.api.task_ref import TaskReadError, project_tasks
from fact.core.enums import NotificationKind
from fact.core.github import GitHubError
from fact.core.holds import failure_cap_reason, held_text, held_title
from fact.core.logging import get_logger
from fact.core.models import Notification
from fact.core.project_config import ConfigError
from fact.core.scheduler import Hold, ReadyTask
from fact.core.settings import Settings
from fact.core.tasks import is_issue
from fact.core.tasks.events import hold
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

log = get_logger(__name__)


def held_key(run_id: str) -> str:
    return f"{run_id}:held"


async def held_runs(session: AsyncSession, run_ids: Iterable[str | None]) -> set[str]:
    """Return those of `run_ids` whose streak the tick already held (it has a `<run id>:held` notification)."""
    keys = [held_key(r) for r in run_ids if r]
    if not keys:
        return set()
    found = await session.scalars(select(Notification.dedupe_key).where(Notification.dedupe_key.in_(keys)))
    return {key.removesuffix(":held") for key in found}


async def released(session: AsyncSession, tasks: Sequence[ReadyTask]) -> list[ReadyTask]:
    """
    Clear FAILURE_CAP on the issues a person released: held by the tick, and no longer carrying `fact:needs-human`.

    A labelled issue shows NEEDS_HUMAN instead, so FAILURE_CAP on an issue means the label is not there.
    """
    capped = [t for t in tasks if t.hold == Hold.FAILURE_CAP and is_issue(t.task_key)]
    done = await held_runs(session, (t.last_run_id for t in capped))
    return [t.model_copy(update={"hold": None}) if t in capped and t.last_run_id in done else t for t in tasks]


async def _hold_one(settings: Settings, session: AsyncSession, task: ReadyTask) -> None:
    ref = next(r for r in settings.projects if r.name == task.project)
    reason = failure_cap_reason(task.failed_runs)
    async with project_tasks(settings, ref) as tasks:
        if tasks is None:
            return
        await hold(tasks.source, task.task_key, held_text(task.task_key, reason))
    session.add(
        Notification(
            run_id=task.last_run_id,
            kind=NotificationKind.TASK_HELD,
            title=held_title(task.task_key, reason)[:300],
            body=held_text(task.task_key, reason),
            dedupe_key=held_key(task.last_run_id or ""),
        )
    )
    await session.commit()
    log.info("task_held", task_key=task.task_key, project=task.project, failed_runs=task.failed_runs)


async def hold_failure_capped(settings: Settings, session: AsyncSession, tasks: Sequence[ReadyTask]) -> None:
    """Label, comment and notify each failure-capped issue (from `find_ready`, after `released`); best effort."""
    for task in (t for t in tasks if t.hold == Hold.FAILURE_CAP and t.last_run_id and is_issue(t.task_key)):
        try:
            await _hold_one(settings, session, task)
        except (ConfigError, TaskReadError, GitHubError, httpx.HTTPError, IntegrityError) as exc:
            await session.rollback()
            log.warning("task_hold_failed", task_key=task.task_key, error=str(exc))
