"""
`fact reset`: wipe the factory's data under `data_dir` to start from scratch.

It deletes the factory database, `temporal.db`, the artifact store, `step-logs/`, `workspaces/`, `repos/`, `pinned/`
and `run/`, then runs `git worktree prune` in the source tree (the factory pins were its worktrees). `.env`, the
source tree and anything else in `data_dir` stay. It refuses while `fact dev` holds its lock (and holds it itself
meanwhile, so none starts; `--dry-run` only checks it), and while a recorded factory process still runs (`fact shutdown` stops them). GitHub keeps
its labels: issues held with `fact:needs-human` stay held.
"""

from pathlib import Path
from typing import Annotated

import typer
from fact.cli.cleanup import prune_worktrees
from fact.cli.cleanup_pins import live_records
from fact.cli.dev import run_dir
from fact.cli.dev_lock import LOCK_NAME, PIN_NAME, AlreadyRunningError, DevLock
from fact.cli.removal import Report, delete
from fact.cli.retention import STEP_LOGS
from fact.core.settings import REPO_ROOT, Settings, get_settings

SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


def _with_sidecars(db: Path) -> list[Path]:
    return [db, *(db.with_name(db.name + suffix) for suffix in SQLITE_SIDECARS)]


def factory_data(settings: Settings) -> list[tuple[Path, str]]:
    """Every path `fact reset` deletes, with what it is."""
    data = settings.data_dir
    paths = [(p, "the factory database") for p in _with_sidecars(settings.db_path)]
    paths += [(p, "Temporal's database (every workflow history)") for p in _with_sidecars(settings.temporal_db_path)]
    paths += [
        (settings.artifacts_dir, "the artifact store"),
        (data / STEP_LOGS, "the step logs"),
        (settings.workspaces_dir, "the runs' workspaces"),
        (data / "repos", "the clones"),
        (settings.pinned_dir, "the pinned versions"),
        (data / "run", "the process records, logs and generations"),
        (data / PIN_NAME, "the pin `fact dev` last ran from"),
    ]
    return paths


def wipe(settings: Settings, report: Report, repo: Path | None = None) -> None:
    """Delete the factory data, prune the source tree's worktrees and say what stays."""
    repo = repo or REPO_ROOT
    targets = factory_data(settings)
    for path, what in targets:
        if path.exists():
            report.remove(str(path), what, lambda p=path: delete(p))
    prune_worktrees(repo, report)
    wiped = {p for p, _ in targets} | {settings.data_dir / LOCK_NAME}
    others = sorted(p for p in settings.data_dir.iterdir() if p not in wiped) if settings.data_dir.is_dir() else []
    for path in others:
        report.keep(str(path), "not factory data")
    report.keep(f"{repo} and its .env", "the source tree is never touched")
    report.note(
        "GitHub keeps its labels: issues held with `fact:needs-human` stay held until the label is removed "
        "(or `fact run` starts them)."
    )


def _refuse(message: str) -> None:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(1)


def reset(
    *,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print what would be deleted; change nothing.")] = False,
) -> None:
    """Delete the factory's data (database, Temporal, artifacts, logs, workspaces, pins) to start from scratch."""
    settings = get_settings()
    lock = DevLock(settings.data_dir / LOCK_NAME)
    stop_first = "stop it (`fact shutdown`) before `fact reset`"
    if dry_run:  # only look: taking the lock would create and write the lock file
        if lock.held():
            _refuse(f"fact dev is already running (holding {lock.path}); {stop_first}")
    else:
        try:
            lock.acquire()
        except AlreadyRunningError as exc:
            _refuse(f"{exc}; {stop_first}")
    try:
        if live := live_records(run_dir(settings)):
            _refuse(f"factory processes still run ({', '.join(live)}); stop them with `fact shutdown` first")
        if not (yes or dry_run):
            typer.confirm(f"Delete every run, log, workspace and pin under {settings.data_dir}?", abort=True)
        report = Report(dry_run=dry_run)
        wipe(settings, report)
    finally:
        lock.release()
    if report.failures:
        raise typer.Exit(1)
