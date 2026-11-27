"""`fact dev`: Temporal dev server, API and worker in one command."""

import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated

import typer
from fact.cli.follow import Follower
from fact.cli.supervisor import INTERRUPTED, ChildSpec, emit, run_all
from fact.cli.use import PinError, current_pin, pin
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


def factory_commands(
    settings: Settings, pinned: Path | None = None, environ: Mapping[str, str] | None = None
) -> list[ChildSpec]:
    """
    Return the API and worker processes; the worker waits for Temporal's port.

    With `pinned`, they run from that checkout's venv and read its own `fact.yaml` and `.env` (copied by `fact use`).
    Either way they start without the venv activation `fact dev` may have inherited, so the workspaces they create
    see a plain environment.
    """
    python = python_argv(pinned)
    env: dict[str, str] = {}
    path = deactivated_path(os.environ if environ is None else environ)
    if path is not None:
        env["PATH"] = path
    return [
        ChildSpec("api", [*python, "-m", "fact.api"], env=env, unset=VENV_VARS),
        ChildSpec(
            "worker", [*python, "-m", "fact.worker"], wait_for=temporal_endpoint(settings), env=env, unset=VENV_VARS
        ),
    ]


def build_commands(
    settings: Settings, temporal_exe: str, pinned: Path | None = None, environ: Mapping[str, str] | None = None
) -> list[ChildSpec]:
    """Return the processes `fact dev` starts, in order: Temporal, then `factory_commands`."""
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
    return [ChildSpec("temporal", temporal), *factory_commands(settings, pinned, environ)]


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


def _follower(settings: Settings, ref: str, checkout: Path, width: int) -> Follower:
    uv = shutil.which("uv") or "uv"
    return Follower(
        settings,
        ref,
        uv=uv,
        pinned=checkout.name,
        commands=lambda c: factory_commands(settings, c),
        log=lambda line: emit("fact", width, line),
    )


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
    """Start Temporal, the API and the worker; Ctrl-C stops all three."""
    temporal_exe = shutil.which("temporal")
    if temporal_exe is None:
        typer.echo(
            "error: `temporal` is not on PATH. Install the Temporal CLI: https://docs.temporal.io/cli#install",
            err=True,
        )
        raise typer.Exit(1)
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if follow is not None:
        _follow_pin(settings, follow)
    checkout = _pinned_or_exit(settings) if pinned or follow is not None else None
    if checkout is not None:
        typer.echo(f"Running pinned {checkout.name} from {checkout}")
    typer.echo(f"Temporal UI: http://localhost:{TEMPORAL_UI_PORT}  API: {settings.api_url}")
    commands = build_commands(settings, temporal_exe, checkout)
    if follow is not None and checkout is not None:
        typer.echo(f"Following {follow}: upgrading once it moves and no run is active")
        width = max(len(c.name) for c in commands)  # the supervisor's prefix width
        code = run_all(commands, _follower(settings, follow, checkout, width))
    else:
        code = run_all(commands)
    raise typer.Exit(0 if code == INTERRUPTED else code)
