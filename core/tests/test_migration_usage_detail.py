"""The `usage_detail` migration adds the cache and partial columns and NULLs the cost of steps without a harness."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from fact.core.db import database_url
from fact.core.migrate import alembic_config, upgrade
from fact.core.settings import Settings

BEFORE = "b8d4f02c3e51"  # pragma: allowlist secret
USAGE_DETAIL = "c5e1a7d93f20"  # pragma: allowlist secret


def _table(name: str, row: dict[str, object]) -> sa.TableClause:
    return sa.table(name, *(sa.column(key) for key in row))


PROJECT = {"id": 1, "name": "fact", "home_repo": "Kahncode/fact", "created_at": "2026-11-27"}
RUN = {
    "id": "r1",
    "workflow_id": "run-1",
    "project_id": 1,
    "task_key": "t",
    "workflow_type": "w",
    "status": "finished",
    "created_at": "2026-11-27",
    "machine_seconds": 0,
    "human_wait_seconds": 0,
    "tokens_in": 300,
    "tokens_out": 20,
    "cost_usd": 1.5,
    "human_touches": 0,
}
AGENT_STEP = {
    "id": "s1",
    "run_id": "r1",
    "stage_id": "implement",
    "kind": "agent",
    "attempt": 1,
    "status": "finished",
    "harness": "claude-code",
    "wait_seconds": 0,
    "tokens_in": 300,
    "tokens_out": 20,
    "cost_usd": 1.5,
}
SHELL_STEP = AGENT_STEP | {"id": "s2", "stage_id": "checks", "kind": "shell", "harness": None}
SHELL_STEP |= {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0}  # the 0 a step without a harness used to get


def _query(sync_url: str, sql: str) -> list[tuple[object, ...]]:
    engine = sa.create_engine(sync_url)
    try:
        with engine.connect() as conn:
            return [tuple(row) for row in conn.execute(sa.text(sql))]
    finally:
        engine.dispose()


def _seed(sync_url: str) -> None:
    engine = sa.create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.execute(_table("projects", PROJECT).insert().values(PROJECT))
            conn.execute(_table("runs", RUN).insert().values(RUN))
            conn.execute(_table("steps", AGENT_STEP).insert(), [AGENT_STEP, SHELL_STEP])
    finally:
        engine.dispose()


def test_usage_detail_upgrade_and_downgrade(tmp_path: Path) -> None:
    url = database_url(Settings(data_dir=tmp_path))
    sync_url = url.replace("sqlite+aiosqlite", "sqlite")
    upgrade(url, BEFORE)
    _seed(sync_url)

    upgrade(url, USAGE_DETAIL)
    steps = _query(
        sync_url,
        "SELECT id, cost_usd, cache_read_tokens, cache_write_tokens, usage_partial FROM steps ORDER BY id",
    )
    assert steps == [("s1", 1.5, 0, 0, 0), ("s2", None, 0, 0, 0)]  # the agent's reported cost stays
    runs = _query(sync_url, "SELECT cache_read_tokens, cache_write_tokens, cost_unknown_steps, cost_usd FROM runs")
    assert runs == [(0, 0, 0, 1.5)]

    command.downgrade(alembic_config(url), BEFORE)
    assert _query(sync_url, "SELECT id, cost_usd FROM steps ORDER BY id") == [("s1", 1.5), ("s2", 0)]
