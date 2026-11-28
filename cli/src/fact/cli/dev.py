"""
`fact dev`: Temporal dev server, API and worker in one command.

They run detached, tracked in `<data_dir>/run/` (`fact.cli.processes`), so restarting `fact dev` adopts them and
touches no running work; `<data_dir>/fact-dev.lock` (`fact.cli.dev_lock`) refuses a second `fact dev`. Pinned, it runs
one worker per worker generation that should run (`fact.cli.generations`), and its own CLI from the newest pin
(`fact.cli.relaunch`); from the source tree, one worker.
"""

import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer
from fact.cli import drainage
from fact.cli.console import emit
from fact.cli.dev_commands import (
    TEMPORAL_UI_PORT,
    api_command,
    build_commands,
    plain_env,
    temporal_endpoint,
    worker_command,
)
from fact.cli.dev_lock import LOCK_NAME, AlreadyRunningError, DevLock
from fact.cli.follow import Follower
from fact.cli.generations import Generations
from fact.cli.processes import RunDir
from fact.cli.relaunch import SelfRestart, launch
from fact.cli.supervisor import INTERRUPTED, Restarter, chain, port_open, run_all
from fact.cli.temporal_admin import NamespaceRetention
from fact.cli.use import PinError, current_pin, pin, venv_python
from fact.core.settings import REPO_ROOT, Settings, get_settings


def _pinned_or_exit(settings: Settings) -> Path:
    try:
        checkout = current_pin(settings)
    except PinError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    if not venv_python(checkout).exists():
        typer.echo(f"error: {venv_python(checkout)} is missing; run `fact use` again", err=True)
        raise typer.Exit(1)
    return checkout


def _follow_pin(settings: Settings, ref: str) -> Path:
    """Start on the current pin, whose generation is newest; pin `ref` if none (the follower upgrades from there)."""
    try:
        return current_pin(settings)
    except PinError:
        pass
    uv = shutil.which("uv")
    if uv is None:
        typer.echo("error: `uv` is not on PATH", err=True)
        raise typer.Exit(1)
    try:
        return pin(settings, ref, uv)
    except PinError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


def _follower(settings: Settings, ref: str, workers: Generations, source: Path, log: Callable[[str], None]) -> Follower:
    uv = shutil.which("uv") or "uv"
    return Follower(settings, ref, uv=uv, workers=workers, log=log, repo=source)


def pin_ready(checkout: Path) -> bool:
    return (checkout / "pyproject.toml").is_file() and venv_python(checkout).exists()


def generations(settings: Settings, log: Callable[[str], None]) -> Generations:
    def in_use(versions: list[drainage.Version]) -> set[drainage.Version] | None:
        if versions and not port_open(*temporal_endpoint(settings)):
            return None  # Temporal is not up yet: ask at the next check
        return drainage.versions_in_use(settings, versions)

    return Generations(
        settings,
        worker_spec=lambda checkout, generation: worker_command(settings, checkout, generation),
        api_spec=lambda checkout: api_command(settings, checkout),
        in_use=in_use,
        ready=pin_ready,
        log=log,
    )


def run_dir(settings: Settings) -> RunDir:
    return RunDir(settings.data_dir / "run")


def _supervise(  # noqa: PLR0913
    settings: Settings,
    temporal_exe: str,
    follow: str | None,
    *,
    pinned: bool,
    source: Path = REPO_ROOT,
    launcher: int | None = None,
) -> int:
    """Supervise here; `source` is the factory source tree (`fact use` fetches there), `launcher` the launching pid."""
    if follow is not None:
        _follow_pin(settings, follow)
    checkout = _pinned_or_exit(settings) if pinned or follow is not None else None
    if checkout is not None:
        typer.echo(f"Running pinned {checkout.name} from {checkout}")
    typer.echo(f"Temporal UI: http://localhost:{TEMPORAL_UI_PORT}  API: {settings.api_url}")
    width = len("fact")

    def log(line: str) -> None:
        emit("fact", width, line)

    retention = NamespaceRetention(settings, temporal_endpoint(settings), log)
    if checkout is None:
        return run_all(build_commands(settings, temporal_exe), run_dir(settings), retention)
    workers = generations(settings, log)
    commands = build_commands(settings, temporal_exe, checkout, workers=workers.initial(checkout, run_dir(settings)))
    width = max(len(c.name) for c in commands)  # the supervisor's prefix width
    running = REPO_ROOT.name if launcher is not None else None  # a launched `fact dev` runs from its pin
    restarters: list[Restarter] = [
        SelfRestart(settings, running=running, repo=source, log=log, launcher=launcher),
        retention,
    ]
    if follow is not None:
        typer.echo(f"Following {follow}: a new generation each time it or a project's home branch moves")
        restarters.append(_follower(settings, follow, workers, source, log))
    return run_all(commands, run_dir(settings), chain(*restarters, workers), workers.exited)


def _locked(settings: Settings, body: Callable[[], int]) -> int:
    lock = DevLock(settings.data_dir / LOCK_NAME)
    try:
        lock.acquire()
    except AlreadyRunningError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    try:
        return body()
    finally:
        lock.release()


def _launch(settings: Settings, temporal_exe: str, follow: str | None, *, pinned: bool) -> int:
    """Run `fact dev` from the newest pin each time it asks (`fact.cli.relaunch`); supervise here for an older pin."""
    _locked(settings, lambda: 0)  # refuse at once while another one runs, before pinning anything
    if follow is not None:
        _follow_pin(settings, follow)
    _pinned_or_exit(settings)
    mode = ["--follow", follow] if follow is not None else ["--pinned"]
    hidden = ["--in-pin", "--source", str(REPO_ROOT), "--launcher", str(os.getpid())]

    def child_argv(checkout: Path) -> list[str]:
        return [str(venv_python(checkout)), "-m", "fact.cli.main", "dev", *mode, *hidden]

    def supervise() -> int:
        return _locked(settings, lambda: _supervise(settings, temporal_exe, follow, pinned=pinned))

    try:
        return launch(settings, child_argv, plain_env(os.environ), supervise)
    except PinError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


def dev(
    *,
    pinned: Annotated[
        bool, typer.Option("--pinned", help="Run the API and worker from the version pinned with `fact use`.")
    ] = False,
    follow: Annotated[
        str | None,
        typer.Option(
            "--follow",
            metavar="REF",
            help="Run pinned and add a worker generation each time REF or a project's home branch moves.",
        ),
    ] = None,
    in_pin: Annotated[bool, typer.Option("--in-pin", hidden=True)] = False,
    source: Annotated[Path | None, typer.Option("--source", hidden=True)] = None,
    launcher: Annotated[int | None, typer.Option("--launcher", hidden=True)] = None,
) -> None:
    """
    Start Temporal, the API and the worker, or adopt those still running; keep them running.

    Ctrl-C stops all three, and so does `fact shutdown`; open runs carry on at the next start. Killing `fact dev`
    leaves them running.
    """
    temporal_exe = shutil.which("temporal")
    if temporal_exe is None:
        typer.echo(
            "error: `temporal` is not on PATH. Install the Temporal CLI: https://docs.temporal.io/cli#install",
            err=True,
        )
        raise typer.Exit(1)
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if (pinned or follow is not None) and not in_pin:
        code = _launch(settings, temporal_exe, follow, pinned=pinned)
    else:
        tree = source or REPO_ROOT

        def body() -> int:
            return _supervise(settings, temporal_exe, follow, pinned=pinned, source=tree, launcher=launcher)

        code = _locked(settings, body)
    raise typer.Exit(0 if code == INTERRUPTED else code)
