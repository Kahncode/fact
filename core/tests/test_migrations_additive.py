"""Every migration is additive or declares `breaking = True`, so two generations can share the database."""

from pathlib import Path

import pytest
from fact.core.migration_rules import breaking_between, breaking_ops, undeclared

VERSIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"
HEADER = 'revision = "{rev}"\ndown_revision = {down}\n'


@pytest.mark.parametrize("path", sorted(VERSIONS.glob("*.py")), ids=lambda p: p.stem)
def test_a_breaking_migration_declares_it(path: Path) -> None:
    assert undeclared(path) == [], f"{path.name} is breaking: set `breaking = True` or make it additive"


def _migration(folder: Path, rev: str, down: str | None, body: str, *, breaking: bool = False) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{rev}.py"
    flag = "breaking = True\n" if breaking else ""
    down_literal = "None" if down is None else repr(down)
    path.write_text(f"{HEADER.format(rev=rev, down=down_literal)}{flag}\ndef upgrade():\n{body}\n", encoding="utf-8")
    return path


def test_a_dropped_column_fails_until_the_migration_says_breaking(tmp_path: Path) -> None:
    body = '    op.drop_column("runs", "trigger")'

    assert undeclared(_migration(tmp_path / "a", "r1", None, body)) == ["line 5: drop_column"]
    assert undeclared(_migration(tmp_path / "b", "r1", None, body, breaking=True)) == []


@pytest.mark.parametrize(
    ("body", "breaking"),
    [
        ('    op.create_table("t", sa.Column("id", sa.Integer(), nullable=False))', False),
        ('    batch.add_column(sa.Column("n", sa.Integer(), nullable=True))', False),
        ('    batch.add_column(sa.Column("n", sa.Integer(), server_default="0", nullable=False))', False),
        ('    batch.add_column(sa.Column("n", sa.Integer(), nullable=False))', True),
        ('    batch.alter_column("cost", nullable=True)', True),
        ('    op.rename_table("a", "b")', True),
        ('    op.drop_table("a")', True),
        ("    for c in cols():\n        batch.add_column(c)", True),  # unreadable: declare the Column inline
    ],
)
def test_what_counts_as_breaking(body: str, *, breaking: bool) -> None:
    assert bool(breaking_ops(f"def upgrade():\n{body}\n")) is breaking


def test_an_upgrade_is_breaking_when_a_migration_between_the_heads_is(tmp_path: Path) -> None:
    old, new = tmp_path / "old" / "core/migrations/versions", tmp_path / "new" / "core/migrations/versions"
    for folder in (old, new):
        _migration(folder, "r1", None, '    op.drop_table("x")', breaking=True)
        _migration(folder, "r2", "r1", "    pass")
    _migration(new, "r3", "r2", '    op.drop_column("t", "c")', breaking=True)
    _migration(new, "r4", "r3", '    op.create_table("t2")')

    assert breaking_between(tmp_path / "old", tmp_path / "new") == ["r3"]  # r1 ran already
    assert breaking_between(tmp_path / "new", tmp_path / "new") == []
    assert breaking_between(tmp_path / "new", tmp_path / "old") == ["r4 is not an ancestor of r2"]  # a rollback
