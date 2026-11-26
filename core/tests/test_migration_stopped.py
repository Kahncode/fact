"""The `stopped_outcome` migration rewrites stored `dropped` outcomes to `stopped`, and back on downgrade."""

from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from fact.core.db import database_url
from fact.core.migrate import alembic_config, upgrade
from fact.core.settings import Settings

BEFORE = "a7c3e91b2d40"  # pragma: allowlist secret
STOPPED_OUTCOME = "b8d4f02c3e51"  # pragma: allowlist secret


# Plain tables with only the columns this test touches: the ORM models follow the newest schema, not this revision's.
RUNS = sa.table(
    "runs",
    sa.column("id", sa.String),
    sa.column("workflow_id", sa.String),
    sa.column("project_id", sa.Integer),
    sa.column("task_key", sa.String),
    sa.column("workflow_type", sa.String),
    sa.column("status", sa.String),
    sa.column("outcome", sa.String),
    sa.column("created_at", sa.DateTime),
    sa.column("machine_seconds", sa.Float),
    sa.column("human_wait_seconds", sa.Float),
    sa.column("tokens_in", sa.Integer),
    sa.column("tokens_out", sa.Integer),
    sa.column("cost_usd", sa.Numeric),
    sa.column("human_touches", sa.Integer),
)
STEPS = sa.table(
    "steps",
    sa.column("id", sa.String),
    sa.column("run_id", sa.String),
    sa.column("stage_id", sa.String),
    sa.column("kind", sa.String),
    sa.column("attempt", sa.Integer),
    sa.column("status", sa.String),
    sa.column("outcome", sa.String),
    sa.column("wait_seconds", sa.Float),
    sa.column("tokens_in", sa.Integer),
    sa.column("tokens_out", sa.Integer),
    sa.column("cost_usd", sa.Numeric),
)
PROJECTS = sa.table(
    "projects",
    sa.column("id", sa.Integer),
    sa.column("name", sa.String),
    sa.column("home_repo", sa.String),
    sa.column("created_at", sa.DateTime),
)
ZERO = {"machine_seconds": 0, "human_wait_seconds": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0}


def _outcomes(sync_url: str) -> tuple[list[str | None], list[str | None]]:
    engine = sa.create_engine(sync_url)
    try:
        with engine.connect() as conn:
            runs = list(conn.scalars(sa.select(RUNS.c.outcome).order_by(RUNS.c.workflow_id)))
            steps = list(conn.scalars(sa.select(STEPS.c.outcome).order_by(STEPS.c.stage_id)))
    finally:
        engine.dispose()
    return runs, steps


def _run(run_id: str, workflow_id: str, outcome: str) -> dict[str, object]:
    return {
        "id": run_id,
        "workflow_id": workflow_id,
        "project_id": 1,
        "task_key": "t",
        "workflow_type": "w",
        "status": "finished",
        "outcome": outcome,
        "created_at": datetime.now(UTC),
        "human_touches": 0,
        **ZERO,
    }


def _step(stage_id: str, outcome: str) -> dict[str, object]:
    step = {"id": stage_id, "run_id": "r1", "stage_id": stage_id, "kind": "agent", "attempt": 1}
    return {**step, "status": "finished", "outcome": outcome, "wait_seconds": 0, "tokens_in": 0, "tokens_out": 0}


def _seed(sync_url: str) -> None:
    engine = sa.create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                PROJECTS.insert().values(id=1, name="fact", home_repo="Kahncode/fact", created_at=datetime.now(UTC))
            )
            conn.execute(RUNS.insert(), [_run("r1", "run-1", "dropped"), _run("r2", "run-2", "merged")])
            conn.execute(
                STEPS.insert(), [{**_step("a", "dropped"), "cost_usd": 0}, {**_step("b", "passed"), "cost_usd": 0}]
            )
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
