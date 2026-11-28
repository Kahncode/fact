"""
`fact cleanup --older-than <duration>`: delete the files of runs that ended before the cutoff, keep their rows.

A run's files are its artifacts (`runs/<run_id>/` in the artifact store) and its step logs (`step-logs/<run_id>/`
under `data_dir`). Its rows in the factory database stay (runs, steps, costs, notifications), so cost history
survives; a step's artifact reference then points at nothing and reads answer 404. A run still open (not finished, or
with no end time) is never touched. The database is read directly, so this works whether or not the API runs.
"""

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import typer
from fact.cli.removal import Report, delete
from fact.core.artifacts import InvalidArtifactKeyError, LocalArtifactStore, run_prefix
from fact.core.db import create_engine, database_url, session_factory
from fact.core.enums import RunStatus
from fact.core.models import Run
from fact.core.settings import Settings
from sqlalchemy import func, select

STEP_LOGS = "step-logs"
_DURATION = re.compile(r"^(\d+)([smhdw])$")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def parse_duration(text: str) -> timedelta:
    """Parse `<n><unit>`, unit one of s, m, h, d, w (such as `30d`)."""
    match = _DURATION.match(text.strip().lower())
    if match is None or int(match.group(1)) == 0:
        msg = f"invalid duration {text!r}: use a positive number and a unit s, m, h, d or w, such as 30d"
        raise typer.BadParameter(msg)
    return timedelta(**{_UNITS[match.group(2)]: int(match.group(1))})


@dataclass(frozen=True)
class Runs:
    ended: list[str]  # ids of the runs that ended before the cutoff
    open: int  # runs not ended, never touched


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)  # SQLite returns naive UTC


async def _runs(settings: Settings, cutoff: datetime) -> Runs:
    engine = create_engine(database_url(settings))
    try:
        async with session_factory(engine)() as session:
            rows = (
                await session.execute(
                    select(Run.id, Run.ended_at).where(Run.status == RunStatus.FINISHED, Run.ended_at.is_not(None))
                )
            ).all()
            open_runs = await session.scalar(
                select(func.count()).select_from(Run).where((Run.status != RunStatus.FINISHED) | Run.ended_at.is_(None))
            )
    finally:
        await engine.dispose()
    ended = sorted(run_id for run_id, ended_at in rows if _utc(ended_at) < cutoff)
    return Runs(ended, open_runs or 0)


def ended_runs(settings: Settings, cutoff: datetime) -> Runs:
    """Return the runs that ended before `cutoff`, and how many are open; none without a database."""
    if not settings.db_path.is_file():
        return Runs([], 0)
    return asyncio.run(_runs(settings, cutoff))


def clean_old_runs(settings: Settings, older_than: timedelta, report: Report, now: datetime | None = None) -> None:
    """Delete the artifacts and step logs of every run that ended over `older_than` ago."""
    cutoff = (now or datetime.now(UTC)) - older_than
    runs = ended_runs(settings, cutoff)
    store = LocalArtifactStore(settings.artifacts_dir)
    logs = settings.data_dir / STEP_LOGS
    for run_id in runs.ended:
        try:
            artifacts = store.path_for(run_prefix(run_id))
        except InvalidArtifactKeyError:
            continue  # not an id of ours: never a path outside the store
        why = f"run {run_id} ended before {cutoff:%Y-%m-%d %H:%M} UTC"
        found = [(f"artifacts of run {run_id}", artifacts), (f"step logs of run {run_id}", logs / run_id)]
        for label, path in found:
            if path.exists():
                report.remove(label, why, lambda p=path: delete(p))
    report.note(
        f"{len(runs.ended)} run(s) ended before the cutoff; their database rows (runs, steps, costs, notifications) "
        "are kept, so `fact runs` still lists them with their costs"
    )
    if runs.open:
        report.keep(f"the files of {runs.open} open run(s)", "a run still open is never touched")
