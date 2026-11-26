"""
stopped outcome: runs and steps are stopped, not dropped.

Revision ID: b8d4f02c3e51
Revises: a7c3e91b2d40
Create Date: 2026-11-26 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8d4f02c3e51"  # pragma: allowlist secret
down_revision: str | None = "a7c3e91b2d40"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("runs", "steps")


def _rename_outcome(old: str, new: str) -> None:
    for name in TABLES:
        table = sa.table(name, sa.column("outcome", sa.String))
        op.execute(table.update().where(table.c.outcome == old).values(outcome=new))


def upgrade() -> None:
    _rename_outcome("dropped", "stopped")


def downgrade() -> None:
    _rename_outcome("stopped", "dropped")
