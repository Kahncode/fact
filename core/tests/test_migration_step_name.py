"""The `step_name` migration adds a nullable `steps.name`, NULL on the steps already stored."""

from pathlib import Path

import sqlalchemy as sa
from fact.core.db import database_url
from fact.core.migrate import upgrade
from fact.core.settings import Settings

BEFORE = "c5e1a7d93f20"  # pragma: allowlist secret
STEP_NAME = "d6f2b8a04e31"  # pragma: allowlist secret

ROWS = {
    "projects": {"id": 1, "name": "fact", "home_repo": "Kahncode/fact", "created_at": "2026-11-27"},
    "runs": {
        "id": "r1",
        "workflow_id": "run-1",
        "project_id": 1,
        "task_key": "t",
        "workflow_type": "w",
        "status": "finished",
        "created_at": "2026-11-27",
        "machine_seconds": 0,
        "human_wait_seconds": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_usd": 0,
        "human_touches": 0,
    },
    "steps": {
        "id": "s1",
        "run_id": "r1",
        "stage_id": "checks",
        "kind": "shell",
        "attempt": 1,
        "status": "finished",
        "wait_seconds": 0,
        "tokens_in": 0,
        "tokens_out": 0,
    },
}


def test_existing_steps_get_a_null_name(tmp_path: Path) -> None:
    url = database_url(Settings(data_dir=tmp_path))
    engine = sa.create_engine(url.replace("sqlite+aiosqlite", "sqlite"))
    try:
        upgrade(url, BEFORE)
        with engine.begin() as conn:
            for table, row in ROWS.items():
                conn.execute(sa.table(table, *(sa.column(key) for key in row)).insert().values(row))

        upgrade(url, STEP_NAME)
        with engine.connect() as conn:
            assert list(conn.execute(sa.text("SELECT id, name FROM steps"))) == [("s1", None)]
    finally:
        engine.dispose()
