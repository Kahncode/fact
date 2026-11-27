"""
step name: a short label telling apart the steps of one stage (`lint` and `test` in `checks`).

Revision ID: d6f2b8a04e31
Revises: c5e1a7d93f20
Create Date: 2026-11-27 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d6f2b8a04e31"  # pragma: allowlist secret
down_revision: str | None = "c5e1a7d93f20"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("steps") as batch:
        batch.add_column(sa.Column("name", sa.String(length=100), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("steps") as batch:
        batch.drop_column("name")
