"""
Removing factory data on disk for `fact cleanup` and `fact reset`, and saying what was removed, what was kept and why.

A pinned checkout is renamed `.trash-<name>` before it is deleted, so a folder deleted half way is never taken for a
pin (pins are found by SHA); a `.trash-` folder that cannot be deleted yet (a file still held on Windows) stays for
the next `fact cleanup`.
"""

import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import typer

TRASH_PREFIX = ".trash-"


def make_writable_and_retry(func: Callable[[str], object], path: str, _exc: BaseException) -> None:
    """Git leaves read-only files (`.git`, objects); on Windows `rmtree` needs them writable."""
    Path(path).chmod(stat.S_IWRITE)
    func(path)


def delete(path: Path) -> str | None:
    """Delete a file or folder; return why it could not be, or None once it is gone."""
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, onexc=make_writable_and_retry)
        else:
            path.unlink(missing_ok=True)
    except OSError as exc:
        return str(exc)
    return None


def trash_path(path: Path) -> Path:
    """Return a free `.trash-<name>` (then `.trash-<name>-<n>`) next to `path`."""
    candidate = path.with_name(f"{TRASH_PREFIX}{path.name}")
    n = 1
    while candidate.exists():
        candidate = path.with_name(f"{TRASH_PREFIX}{path.name}-{n}")
        n += 1
    return candidate


def is_trash(path: Path) -> bool:
    return path.name.startswith(TRASH_PREFIX)


@dataclass
class Report:
    """Prints each removal (or, dry, what it would remove) and each thing kept, with why; counts the failures."""

    dry_run: bool
    echo: Callable[[str], None] = typer.echo
    failures: int = 0
    removed: list[str] = field(default_factory=list)

    def remove(self, what: str, why: str, action: Callable[[], str | None]) -> bool:
        """Run `action` (it returns an error or None) unless dry; return whether `what` is gone (or would be)."""
        if self.dry_run:
            self.echo(f"would remove {what}: {why}")
            self.removed.append(what)
            return True
        if (error := action()) is not None:
            self.failures += 1
            self.echo(f"could not remove {what}: {error}")
            return False
        self.echo(f"removed {what}: {why}")
        self.removed.append(what)
        return True

    def keep(self, what: str, why: str) -> None:
        self.echo(f"kept {what}: {why}")

    def note(self, line: str) -> None:
        self.echo(line)


def remove_checkout(report: Report, path: Path, label: str, why: str) -> None:
    """Rename a pinned checkout `.trash-<name>`, then delete it; a trash folder that stays is left for next time."""

    def action() -> str | None:
        trash = trash_path(path)
        try:
            path.rename(trash)
        except OSError as exc:
            return f"cannot rename it (still in use?): {exc}"
        if (error := delete(trash)) is not None:
            return f"left as {trash.name} for the next `fact cleanup`: {error}"
        return None

    report.remove(label, why, action)
