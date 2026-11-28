"""
`fact cleanup`: remove what old versions leave on disk and in Temporal, and old runs' files; safe at any time.

Old versions (`fact.cli.cleanup_pins`): the pins, Temporal versions and generation records of drained generations,
the `.trash-` folders an earlier cleanup could not delete, and the records of processes that have exited (only while
`fact dev` is not running: a running one owns its records); then `git worktree prune` in the source tree, so the
removed factory pins leave `git worktree list`. With `--older-than`, also the files of runs that ended before the
cutoff (`fact.cli.retention`). `--dry-run` prints what it would do and changes nothing.
"""

import subprocess
from pathlib import Path
from typing import Annotated

import typer
from fact.cli import drainage, temporal_admin
from fact.cli.cleanup_pins import clean_versions, recorded
from fact.cli.dev import run_dir
from fact.cli.dev_lock import LOCK_NAME, DevLock
from fact.cli.processes import RunDir, find_process
from fact.cli.removal import Report
from fact.cli.retention import clean_old_runs, parse_duration
from fact.core.settings import REPO_ROOT, Settings, get_settings


def prune_worktrees(repo: Path, report: Report) -> None:
    """Run `git worktree prune` in the source tree; a failure is printed, not fatal."""
    if report.dry_run:
        report.note(f"would run `git worktree prune` in {repo}")
        return
    try:
        result = subprocess.run(
            ["git", "worktree", "prune"],  # noqa: S607
            cwd=repo,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except OSError as exc:
        report.failures += 1
        report.note(f"could not run `git worktree prune` in {repo}: {exc}")
        return
    if result.returncode != 0:
        report.failures += 1
        report.note(f"`git worktree prune` failed in {repo}: {(result.stderr or result.stdout).strip()}")
        return
    report.note(f"ran `git worktree prune` in {repo}")


def clean_records(settings: Settings, runs: RunDir, report: Report) -> None:
    """Remove the records of exited processes, unless `fact dev` runs (it owns them, and acts on a removed one)."""
    if not runs.path.is_dir():
        return
    if DevLock(settings.data_dir / LOCK_NAME).held():
        report.keep("process records", "`fact dev` is running and keeps its own records")
        return
    for name, record in recorded(runs).items():
        if record is None or find_process(record) is None:
            report.remove(f"process record {name}", "its process has exited", lambda n=name: runs.remove(n))


def run_cleanup(settings: Settings, report: Report, older_than: str | None, repo: Path | None = None) -> None:
    retention = parse_duration(older_than) if older_than is not None else None
    runs = run_dir(settings)
    clean_versions(
        settings,
        runs,
        report,
        in_use=lambda versions: drainage.versions_in_use(settings, versions),
        delete_versions=lambda versions: temporal_admin.delete_versions(settings, versions),
    )
    clean_records(settings, runs, report)
    prune_worktrees(repo or REPO_ROOT, report)
    if retention is not None:
        clean_old_runs(settings, retention, report)


def cleanup(
    *,
    older_than: Annotated[
        str | None,
        typer.Option(
            "--older-than", metavar="DURATION", help="Also delete the files of runs ended before, such as 30d."
        ),
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print what would be removed; change nothing.")] = False,
) -> None:
    """Remove drained versions' pins, Temporal versions and records, and (--older-than) old runs' files."""
    report = Report(dry_run=dry_run)
    run_cleanup(get_settings(), report, older_than)
    if not report.removed:
        report.note("nothing to remove")
    if report.failures:
        raise typer.Exit(1)
