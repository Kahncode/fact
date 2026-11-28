"""
`fact dev`: Temporal dev server, API and worker in one command.

They run detached, tracked in `<data_dir>/run/` (`fact.cli.processes`), so restarting `fact dev` adopts them and
touches no running work; `<data_dir>/fact-dev.lock` (`fact.cli.dev_lock`) refuses a second `fact dev`. Pinned, it runs
one worker per worker generation that should run (`fact.cli.generations`); from the source tree, one worker.
"""

import os
import shutil
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated

import typer
from fact.cli import drainage
from fact.cli.dev_lock import LOCK_NAME, AlreadyRunningError, DevLock
from fact.cli.follow import Follower
from fact.cli.generations import Generations, worker_name
from fact.cli.processes import SOURCE_BUILD, ChildSpec, RunDir
from fact.cli.supervisor import INTERRUPTED, Restarter, chain, emit, port_open, run_all
from fact.cli.use import PinError, current_pin, pin
from fact.core.generations import Generation
from fact.core.settings import Settings, get_settings

TEMPORAL_UI_PORT = 8233
IS_WINDOWS = sys.platform == "win32"
# what activating a venv sets; children get neither, so workspaces never see an activation from VS Code or `uv run`
VENV_VARS = ("VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT")


def temporal_endpoint(settings: Settings) -> tuple[str, int]:
    """Split `temporal.address` (`host:port`, IPv6 hosts in brackets) for the port check."""
    address = settings.temporal.address
    host, _, port = address.rpartition(":")
    if not port.isdigit():
        msg = f"temporal.address must be host:port, got {address!r}"
        raise typer.BadParameter(msg)
    return host.strip("[]") or "localhost", int(port)


def venv_python(checkout: Path) -> Path:
    """Return the Python of a checkout's `.venv`, which `fact use` builds with `uv sync`."""
    return checkout / ".venv" / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def python_argv(pinned: Path | None) -> list[str]:
    """Return the Python to run: this interpreter, or the pinned checkout's venv Python (no `uv run` activation)."""
    return [sys.executable] if pinned is None else [str(venv_python(pinned))]


def deactivated_path(environ: Mapping[str, str]) -> str | None:
    """Return `PATH` without the active venv's scripts folder, or None when no venv is active."""
    venv = environ.get("VIRTUAL_ENV")
    if not venv:
        return None
    scripts = Path(venv) / ("Scripts" if IS_WINDOWS else "bin")
    return os.pathsep.join(p for p in environ.get("PATH", "").split(os.pathsep) if p and Path(p) != scripts)


def _child_env(environ: Mapping[str, str] | None) -> dict[str, str]:
    env: dict[str, str] = {}
    path = deactivated_path(os.environ if environ is None else environ)
    if path is not None:
        env["PATH"] = path
    return env


def api_command(settings: Settings, pinned: Path | None = None, environ: Mapping[str, str] | None = None) -> ChildSpec:
    """
    Return the API process: from the source tree, or from `pinned`'s venv, reading its own `fact.yaml` and `.env`.

    Every factory process starts without the venv activation `fact dev` may have inherited, so the workspaces they
    create see a plain environment.
    """
    build = SOURCE_BUILD if pinned is None else pinned.name
    argv = [*python_argv(pinned), "-m", "fact.api"]
    return ChildSpec("api", argv, build=build, port=settings.api.port, env=_child_env(environ), unset=VENV_VARS)


def worker_command(
    settings: Settings,
    pinned: Path | None = None,
    generation: Generation | None = None,
    environ: Mapping[str, str] | None = None,
) -> ChildSpec:
    """Return a worker process, which waits for Temporal's port: the source tree's, or a generation's from its pin."""
    argv = [*python_argv(pinned), "-m", "fact.worker"]
    name, build = "worker", SOURCE_BUILD if pinned is None else pinned.name
    if generation is not None:
        argv += ["--generation", generation.id]
        name = worker_name(generation)
    wait_for = temporal_endpoint(settings)
    return ChildSpec(name, argv, build=build, wait_for=wait_for, env=_child_env(environ), unset=VENV_VARS)


def build_commands(
    settings: Settings,
    temporal_exe: str,
    pinned: Path | None = None,
    environ: Mapping[str, str] | None = None,
    workers: list[ChildSpec] | None = None,
) -> list[ChildSpec]:
    """Return the processes `fact dev` starts, in order: Temporal, the API, then `workers` (the source tree's one)."""
    _, temporal_port = temporal_endpoint(settings)
    temporal = [
        temporal_exe,
        "server",
        "start-dev",
        "--db-filename",
        str(settings.temporal_db_path),
        "--port",
        str(temporal_port),
        "--ui-port",
        str(TEMPORAL_UI_PORT),
    ]
    api = api_command(settings, pinned, environ)
    workers = [worker_command(settings, environ=environ)] if workers is None else workers
    # killed, not stopped gracefully: it has no stop file to watch
    return [ChildSpec("temporal", temporal, build=api.build, port=temporal_port, graceful=False), api, *workers]


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
    """Start on the current pin, so runs left from the last session finish before an upgrade; pin `ref` if none."""
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


def _follower(settings: Settings, ref: str, checkout: Path, log: Callable[[str], None]) -> Follower:
    uv = shutil.which("uv") or "uv"
    return Follower(settings, ref, uv=uv, pinned=checkout.name, log=log)


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


def _supervise(settings: Settings, temporal_exe: str, follow: str | None, *, pinned: bool) -> int:
    if follow is not None:
        _follow_pin(settings, follow)
    checkout = _pinned_or_exit(settings) if pinned or follow is not None else None
    if checkout is not None:
        typer.echo(f"Running pinned {checkout.name} from {checkout}")
    typer.echo(f"Temporal UI: http://localhost:{TEMPORAL_UI_PORT}  API: {settings.api_url}")
    width = len("fact")

    def log(line: str) -> None:
        emit("fact", width, line)

    if checkout is None:
        return run_all(build_commands(settings, temporal_exe), run_dir(settings))
    workers = generations(settings, log)
    commands = build_commands(settings, temporal_exe, checkout, workers=workers.initial(checkout, run_dir(settings)))
    width = max(len(c.name) for c in commands)  # the supervisor's prefix width
    restarter: Restarter = workers
    if follow is not None:
        typer.echo(f"Following {follow}: upgrading once it moves and no run is active")
        restarter = chain(_follower(settings, follow, checkout, log), workers)
    return run_all(commands, run_dir(settings), restarter)


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
            help="Run pinned and upgrade (`fact use REF`, restart) each time REF moves, once no run is active.",
        ),
    ] = None,
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
    lock = DevLock(settings.data_dir / LOCK_NAME)
    try:
        lock.acquire()
    except AlreadyRunningError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    try:
        code = _supervise(settings, temporal_exe, follow, pinned=pinned)
    finally:
        lock.release()
    raise typer.Exit(0 if code == INTERRUPTED else code)
