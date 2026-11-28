"""
usage detail: cache tokens on runs and steps, partial usage, and NULL for a cost nobody reported.

`steps.cost_usd` becomes nullable: a step's cost is what its harness reported, and NULL when it reported none. Steps
without a harness (shell, GitHub, gates) never had a cost, so their stored 0 becomes NULL. `runs.cost_unknown_steps`
counts the agent steps whose cost is NULL; `runs.cost_usd` stays the sum of the known costs.

Revision ID: c5e1a7d93f20
Revises: b8d4f02c3e51
Create Date: 2026-11-27 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5e1a7d93f20"  # pragma: allowlist secret
down_revision: str | None = "b8d4f02c3e51"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
breaking = True  # `steps.cost_usd` becomes nullable (`fact.core.migration_rules`)

USD = sa.Numeric(precision=12, scale=6)


def _tokens() -> list[sa.Column]:
    return [
        sa.Column("cache_read_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cache_write_tokens", sa.Integer(), server_default="0", nullable=False),
    ]


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        for column in _tokens():
            batch.add_column(column)
        batch.add_column(sa.Column("cost_unknown_steps", sa.Integer(), server_default="0", nullable=False))
    with op.batch_alter_table("steps") as batch:
        for column in _tokens():
            batch.add_column(column)
        batch.add_column(sa.Column("usage_partial", sa.Boolean(), server_default=sa.false(), nullable=False))
        batch.alter_column("cost_usd", existing_type=USD, nullable=True)
    steps = sa.table("steps", sa.column("harness", sa.String), sa.column("cost_usd", USD))
    op.execute(steps.update().where(steps.c.harness.is_(None)).values(cost_usd=None))


def downgrade() -> None:
    steps = sa.table("steps", sa.column("cost_usd", USD))
    op.execute(steps.update().where(steps.c.cost_usd.is_(None)).values(cost_usd=0))
    with op.batch_alter_table("steps") as batch:
        batch.alter_column("cost_usd", existing_type=USD, nullable=False)
        batch.drop_column("usage_partial")
        batch.drop_column("cache_write_tokens")
        batch.drop_column("cache_read_tokens")
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("cost_unknown_steps")
        batch.drop_column("cache_write_tokens")
        batch.drop_column("cache_read_tokens")
