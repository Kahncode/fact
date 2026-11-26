"""The `stopped_outcome` migration rewrites stored `dropped` outcomes to `stopped`, and back on downgrade."""

from pathlib import Path

from alembic import command
from fact.core.db import database_url
from fact.core.migrate import alembic_config, upgrade
from fact.core.models import Project, Run, Step
from fact.core.settings import Settings
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

BEFORE = "a7c3e91b2d40"  # pragma: allowlist secret
STOPPED_OUTCOME = "b8d4f02c3e51"  # pragma: allowlist secret


def _outcomes(sync_url: str) -> tuple[list[str | None], list[str | None]]:
    engine = create_engine(sync_url)
    try:
        with Session(engine) as session:
            runs = list(session.scalars(select(Run.outcome).order_by(Run.workflow_id)))
            steps = list(session.scalars(select(Step.outcome).order_by(Step.stage_id)))
    finally:
        engine.dispose()
    return runs, steps


def _seed(sync_url: str) -> None:
    engine = create_engine(sync_url)
    try:
        with Session(engine) as session:
            project = Project(name="fact", home_repo="Kahncode/fact")
            session.add(project)
            session.flush()
            ended = Run(workflow_id="run-1", project_id=project.id, task_key="t", workflow_type="w", outcome="dropped")
            other = Run(workflow_id="run-2", project_id=project.id, task_key="t", workflow_type="w", outcome="merged")
            session.add_all([ended, other])
            session.flush()
            session.add_all(
                [
                    Step(run_id=ended.id, stage_id="a", kind="agent", outcome="dropped"),
                    Step(run_id=ended.id, stage_id="b", kind="agent", outcome="passed"),
                ]
            )
            session.commit()
    finally:
        engine.dispose()


def test_dropped_outcomes_become_stopped_and_back(tmp_path: Path) -> None:
    url = database_url(Settings(data_dir=tmp_path))
    sync_url = url.replace("sqlite+aiosqlite", "sqlite")
    upgrade(url, BEFORE)
    _seed(sync_url)

    upgrade(url, STOPPED_OUTCOME)
    assert _outcomes(sync_url) == (["stopped", "merged"], ["stopped", "passed"])

    command.downgrade(alembic_config(url), BEFORE)
    assert _outcomes(sync_url) == (["dropped", "merged"], ["dropped", "passed"])
