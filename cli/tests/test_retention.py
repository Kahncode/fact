"""`fact cleanup --older-than`: the files of runs ended before the cutoff go, their rows and open runs stay."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import typer
from fact.cli.removal import Report
from fact.cli.retention import clean_old_runs, parse_duration
from fact.core import migrate
from fact.core.db import create_engine, database_url, session_factory
from fact.core.enums import RunStatus
from fact.core.models import Project, Run, Step
from fact.core.settings import Settings
from sqlalchemy import select

NOW = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)
RUNS = {  # run id: (status, ended how long ago, or None while open)
    "old": (RunStatus.FINISHED, timedelta(days=40)),
    "recent": (RunStatus.FINISHED, timedelta(days=10)),
    "open": (RunStatus.WAITING, None),
}


async def _seed(settings: Settings) -> None:
    engine = create_engine(database_url(settings))
    async with session_factory(engine)() as session:
        session.add(Project(id=1, name="fact", home_repo="Kahncode/fact"))
        for run_id, (status, ago) in RUNS.items():
            ended = None if ago is None else NOW - ago
            session.add(
                Run(
                    id=run_id,
                    workflow_id=f"wf-{run_id}",
                    project_id=1,
                    task_key="task:x",
                    workflow_type="Standard",
                    status=status,
                    created_at=NOW - timedelta(days=60),
                    ended_at=ended,
                    cost_usd=Decimal("1.25"),
                )
            )
            session.add(Step(run_id=run_id, stage_id="implement", kind="agent"))
        await session.commit()
    await engine.dispose()


async def _rows(settings: Settings) -> dict[str, Decimal]:
    engine = create_engine(database_url(settings))
    async with session_factory(engine)() as session:
        costs = dict((await session.execute(select(Run.id, Run.cost_usd))).all())
        steps = len((await session.scalars(select(Step))).all())
    await engine.dispose()
    assert steps == len(RUNS)
    return costs


@pytest.fixture
def seeded(settings: Settings) -> Settings:
    settings.data_dir.mkdir(parents=True)
    migrate.upgrade(database_url(settings))
    asyncio.run(_seed(settings))
    for run_id in RUNS:
        (settings.artifacts_dir / "runs" / run_id / "implement" / "1").mkdir(parents=True)
        (settings.artifacts_dir / "runs" / run_id / "implement" / "1" / "transcript.jsonl").write_text(
            "{}", encoding="utf-8"
        )
        (settings.data_dir / "step-logs" / run_id / "implement" / "1").mkdir(parents=True)
    return settings


def test_older_than_removes_only_the_files_of_runs_ended_before_the_cutoff(seeded: Settings) -> None:
    clean_old_runs(seeded, timedelta(days=30), Report(dry_run=False, echo=lambda _line: None), now=NOW)

    with_files = {
        run_id
        for run_id in RUNS
        if (seeded.artifacts_dir / "runs" / run_id).exists() or (seeded.data_dir / "step-logs" / run_id).exists()
    }
    assert with_files == {"recent", "open"}
    assert asyncio.run(_rows(seeded)) == dict.fromkeys(RUNS, Decimal("1.25"))  # cost history survives


@pytest.mark.parametrize(
    ("text", "expected"), [("30d", timedelta(days=30)), ("12h", timedelta(hours=12)), ("2W", timedelta(weeks=2))]
)
def test_parse_duration(text: str, expected: timedelta) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "30", "d", "0d", "-1d", "30y", "1.5d"])
def test_parse_duration_refuses(text: str) -> None:
    with pytest.raises(typer.BadParameter):
        parse_duration(text)


def test_no_database_is_nothing_to_do(settings: Settings) -> None:
    clean_old_runs(settings, timedelta(days=1), Report(dry_run=False, echo=lambda _line: None), now=NOW)

    assert not settings.db_path.exists()
