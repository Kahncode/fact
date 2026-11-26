"""
run trigger.

Revision ID: a7c3e91b2d40
Revises: f2a4a5e26a47
Create Date: 2026-11-26 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c3e91b2d40"  # pragma: allowlist secret
down_revision: str | None = "f2a4a5e26a47"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("trigger", sa.String(length=20), server_default="manual", nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("trigger")
