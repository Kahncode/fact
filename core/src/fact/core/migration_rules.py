"""
Migrations are additive, so two worker generations can share one database; a breaking one says `breaking = True`.

A migration's `upgrade()` is breaking when it drops or renames a table or column, alters a column (its type,
nullability, default or name), adds a constraint to an existing table, or adds a NOT NULL column without a server
default (`breaking_ops`). Modules are read as source (`ast`), never imported, so the CLI can read another pin's.
`core/tests/test_migrations_additive.py` fails on a breaking migration that does not declare it; the follower
(`fact.cli.follow`) reads the migrations between two pins' heads (`between`) to tell a breaking upgrade.
"""

import ast
from dataclasses import dataclass
from pathlib import Path

VERSIONS = Path("core/migrations/versions")  # in a factory checkout
# `op.<name>` / `batch.<name>` calls breaking whatever their arguments
BREAKING_CALLS = frozenset(
    {
        "drop_table",
        "drop_column",
        "rename_table",
        "alter_column",
        "create_unique_constraint",
        "create_check_constraint",
        "create_foreign_key",
        "create_primary_key",
    }
)


@dataclass(frozen=True)
class Migration:
    revision: str
    down_revisions: tuple[str, ...]
    breaking: bool
    path: Path


def _literal(node: ast.expr) -> object:
    try:
        return ast.literal_eval(node)
    except ValueError:
        return None


def _assigned(tree: ast.Module) -> dict[str, object]:
    """Module-level `name = <literal>` and `name: T = <literal>` assignments."""
    found: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and node.value is not None:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = _literal(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            found[node.target.id] = _literal(node.value)
    return found


def read_migration(path: Path) -> Migration:
    values = _assigned(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    down = values.get("down_revision")
    downs = tuple(down) if isinstance(down, (tuple, list)) else ((down,) if isinstance(down, str) else ())
    return Migration(str(values["revision"]), downs, values.get("breaking") is True, path)


def read_all(versions: Path) -> dict[str, Migration]:
    """Every migration in a versions folder by revision; none when the folder is missing."""
    migrations = [read_migration(p) for p in sorted(versions.glob("*.py"))] if versions.is_dir() else []
    return {m.revision: m for m in migrations}


def head(migrations: dict[str, Migration]) -> str | None:
    """Return the revision no other migration revises; None for no migrations."""
    parents = {d for m in migrations.values() for d in m.down_revisions}
    heads = [r for r in migrations if r not in parents]
    return heads[0] if heads else None


def between(migrations: dict[str, Migration], old_head: str | None, new_head: str | None) -> list[Migration] | None:
    """Return the migrations `new_head` has and `old_head` lacks; None when `old_head` is not an ancestor of it."""
    found: list[Migration] = []
    seen: set[str] = set()
    todo = [new_head] if new_head is not None else []
    reached = old_head is None
    while todo:
        revision = todo.pop()
        if revision in seen:
            continue
        seen.add(revision)
        if revision == old_head:
            reached = True
            continue
        migration = migrations.get(revision)
        if migration is None:
            return None  # a broken chain: nothing can be said
        found.append(migration)
        todo.extend(migration.down_revisions)
    return found if reached else None


def _call_name(node: ast.Call) -> str | None:
    return node.func.attr if isinstance(node.func, ast.Attribute) else None


def _is_column(node: ast.expr) -> bool:
    return isinstance(node, ast.Call) and (
        (isinstance(node.func, ast.Attribute) and node.func.attr == "Column")
        or (isinstance(node.func, ast.Name) and node.func.id == "Column")
    )


def _not_null_without_default(column: ast.Call) -> bool:
    keywords = {k.arg: k.value for k in column.keywords if k.arg is not None}
    not_null = isinstance(keywords.get("nullable"), ast.Constant) and keywords["nullable"].value is False
    default = keywords.get("server_default")
    return not_null and (default is None or (isinstance(default, ast.Constant) and default.value is None))


def _add_column_problem(call: ast.Call) -> str | None:
    columns = [a for a in call.args if _is_column(a)]
    if not columns:
        return "add_column with a column it cannot read (declare the sa.Column inline)"
    if any(_not_null_without_default(c) for c in columns if isinstance(c, ast.Call)):
        return "add_column of a NOT NULL column without a server_default"
    return None


def breaking_ops(source: str) -> list[str]:
    """Return what makes a migration's `upgrade()` breaking, one line each with its line number; [] when additive."""
    tree = ast.parse(source)
    upgrade = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade"), None)
    if upgrade is None:
        return []
    found: list[str] = []
    for node in ast.walk(upgrade):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name in BREAKING_CALLS:
            found.append(f"line {node.lineno}: {name}")
        elif name == "add_column" and (problem := _add_column_problem(node)):
            found.append(f"line {node.lineno}: {problem}")
    return found


def undeclared(path: Path) -> list[str]:
    """Return the breaking operations of a migration that does not set `breaking = True`; [] when it is fine."""
    if read_migration(path).breaking:
        return []
    return breaking_ops(path.read_text(encoding="utf-8"))


def breaking_between(old_checkout: Path, new_checkout: Path) -> list[str]:
    """
    Return the breaking migrations an upgrade from `old_checkout` to `new_checkout` runs; [] when additive.

    When the old head is not an ancestor of the new one (a rollback, a broken chain), the upgrade counts as breaking.
    """
    old_head = head(read_all(old_checkout / VERSIONS))
    migrations = read_all(new_checkout / VERSIONS)
    new_head = head(migrations)
    if old_head == new_head:
        return []
    chain = between(migrations, old_head, new_head)
    if chain is None:
        return [f"{old_head} is not an ancestor of {new_head}"]
    return [m.revision for m in chain if m.breaking]
