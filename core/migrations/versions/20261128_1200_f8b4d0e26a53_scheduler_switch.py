"""
scheduler switch: the dev loop's on/off state (`fact scheduler on|off`), kept across API restarts; no row means on.

Revision ID: f8b4d0e26a53
Revises: e7a3c9d15f42
Create Date: 2026-11-28 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f8b4d0e26a53"  # pragma: allowlist secret
down_revision: str | None = "e7a3c9d15f42"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scheduler_switch",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("on", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scheduler_switch")),
    )


def downgrade() -> None:
    op.drop_table("scheduler_switch")
