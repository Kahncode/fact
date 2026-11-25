from decimal import Decimal
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fact.core.db import Base, create_engine, database_url, session_factory
from fact.core.enums import RunStatus, StepKind, StepOutcome, StepStatus
from fact.core.migrate import upgrade
from fact.core.models import Event, Project, Run, Step
from fact.core.settings import Settings
from sqlalchemy import JSON, DateTime, Float, Integer, Numeric, String, Text
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy.exc import IntegrityError

UUID_HEX_LENGTH = 32


def _url(tmp_path: Path) -> str:
    return database_url(Settings(data_dir=tmp_path))


@pytest.fixture
def url(tmp_path: Path) -> str:
    """URL of a freshly migrated DB (migrated synchronously: Alembic starts its own event loop)."""
    db_url = _url(tmp_path)
    upgrade(db_url)
    return db_url


def _run(project_id: int, n: int = 1) -> Run:
    return Run(workflow_id=f"run-FACT-1-{n}", project_id=project_id, task_key="FACT-1", workflow_type="implement_issue")


async def test_insert_project_run_steps_event(url: str) -> None:
    engine = create_engine(url)
    try:
        async with session_factory(engine)() as session:
            project = Project(name="demo", home_repo="acme/demo")
            session.add(project)
            await session.flush()
            run = _run(project.id)
            session.add(run)
            await session.flush()
            first = Step(run_id=run.id, stage_id="plan", kind=StepKind.AGENT, cost_usd=Decimal("0.123456"))
            second = Step(run_id=run.id, stage_id="plan", kind=StepKind.AGENT, attempt=2)
            session.add_all([first, second])
            await session.flush()
            session.add(Event(run_id=run.id, step_id=first.id, type="step_started", payload={"a": 1}))
            await session.commit()

            await session.refresh(run)
            assert run.status == RunStatus.QUEUED
            assert run.outcome is None
            assert run.cost_usd == Decimal(0)
            assert len(run.id) == UUID_HEX_LENGTH
            assert first.status == StepStatus.QUEUED
            assert first.attempt == 1
            assert first.cost_usd == Decimal("0.123456")
    finally:
        await engine.dispose()


async def test_step_attempt_is_unique_per_run_and_stage(url: str) -> None:
    engine = create_engine(url)
    try:
        async with session_factory(engine)() as session:
            project = Project(name="demo", home_repo="acme/demo")
            session.add(project)
            await session.flush()
            run = _run(project.id)
            session.add(run)
            await session.flush()
            session.add(Step(run_id=run.id, stage_id="plan", kind=StepKind.AGENT, outcome=StepOutcome.PASSED))
            await session.flush()
            session.add(Step(run_id=run.id, stage_id="plan", kind=StepKind.AGENT))
            with pytest.raises(IntegrityError):
                await session.flush()
    finally:
        await engine.dispose()


async def test_workflow_id_and_project_name_are_unique(url: str) -> None:
    engine = create_engine(url)
    try:
        async with session_factory(engine)() as session:
            project = Project(name="demo", home_repo="acme/demo")
            session.add(project)
            await session.flush()
            session.add_all([_run(project.id), _run(project.id)])
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()
            session.add_all([Project(name="dup", home_repo="a/b"), Project(name="dup", home_repo="a/b")])
            with pytest.raises(IntegrityError):
                await session.flush()
    finally:
        await engine.dispose()


def test_migrations_match_models(tmp_path: Path) -> None:
    url = _url(tmp_path)
    upgrade(url)
    engine = create_sync_engine(url.replace("sqlite+aiosqlite", "sqlite"))
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            assert compare_metadata(context, Base.metadata) == []
    finally:
        engine.dispose()


def test_only_portable_column_types() -> None:
    allowed = (String, Text, Integer, Float, Numeric, DateTime, JSON)
    for table in Base.metadata.tables.values():
        for column in table.columns:
            assert type(column.type) in allowed, (table.name, column.name, column.type)


async def test_foreign_keys_are_enforced(url: str) -> None:
    engine = create_engine(url)
    try:
        async with session_factory(engine)() as session:
            session.add(_run(project_id=999))
            with pytest.raises(IntegrityError):
                await session.flush()
    finally:
        await engine.dispose()
