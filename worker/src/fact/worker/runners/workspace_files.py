"""
The run lock (on the worker's disk, for both runners) and `WORKSPACE.md`.

The locks are OS file locks (`filelock`), so they hold across worker processes and are released when a process dies.
Their names start with a dot, which no run id or repository name can, so they never collide with a run.
"""

import stat
from collections.abc import Callable, Mapping
from pathlib import Path

from fact.core.types import WorkspaceSpec
from filelock import AsyncFileLock

WORKSPACE_MD = "WORKSPACE.md"


def folder(full_name: str) -> str:
    return full_name.partition("/")[2]


def run_lock(root: Path) -> AsyncFileLock:
    """
    Return the lock of the run folder `root`, beside it: one `prepare` or `teardown` of a run at a time, anywhere.

    An attempt Temporal gave up on may still hold it (its worker hard-killed while git children ran).
    """
    root.parent.mkdir(parents=True, exist_ok=True)
    return AsyncFileLock(root.with_name(f".{root.name}.lock"))


def make_writable_and_retry(func: Callable[[str], object], path: str, _exc: BaseException) -> None:
    """Git marks object files read-only; on Windows `rmtree` needs them writable."""
    Path(path).chmod(stat.S_IWRITE)
    func(path)


def workspace_md(spec: WorkspaceSpec, branches: Mapping[str, str]) -> str:
    """Return the text of `WORKSPACE.md`: the task and each repository's folder, role and branch."""
    lines = [
        "# Workspace",
        "",
        f"Task: `{spec.task_key}`. Every repository below is checked out side by side on its own branch.",
        "Commit locally; never push.",
        "",
        "| Folder | Repository | Role | Branch |",
        "| --- | --- | --- | --- |",
    ]
    lines.extend(
        f"| `{folder(r.full_name)}` | {r.full_name} | {r.role or '-'} | `{branches[r.full_name]}` |" for r in spec.repos
    )
    return "\n".join(lines) + "\n"


def write_workspace_md(root: Path, spec: WorkspaceSpec, branches: Mapping[str, str]) -> None:
    """Write `WORKSPACE.md` and rename it into place: its presence marks a finished workspace."""
    partial = root / f".{WORKSPACE_MD}.partial"
    partial.write_text(workspace_md(spec, branches), encoding="utf-8", newline="\n")
    partial.replace(root / WORKSPACE_MD)
