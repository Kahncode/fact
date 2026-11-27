"""
step turns and peak context: an agent session's model calls and the input of its largest turn; NULL for other steps.

Revision ID: e7a3c9d15f42
Revises: d6f2b8a04e31
Create Date: 2026-11-27 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7a3c9d15f42"  # pragma: allowlist secret
down_revision: str | None = "d6f2b8a04e31"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("steps") as batch:
        batch.add_column(sa.Column("turns", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("peak_context_tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("steps") as batch:
        batch.drop_column("peak_context_tokens")
        batch.drop_column("turns")
