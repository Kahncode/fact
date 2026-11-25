"""`fact dev`: Temporal dev server, API and worker in one command."""

import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated

import typer
from fact.cli.supervisor import INTERRUPTED, ChildSpec, run_all
from fact.cli.use import PinError, current_pin
from fact.core.settings import CONFIG_VAR, Settings, config_file, get_settings

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


def build_commands(
    settings: Settings, temporal_exe: str, pinned: Path | None = None, environ: Mapping[str, str] | None = None
) -> list[ChildSpec]:
    """
    Return the processes `fact dev` starts, in order; the worker waits for Temporal's port.

    With `pinned`, the API and worker run from that checkout's venv, reading this tree's `fact.yaml` and `.env` (which
    a pinned checkout lacks) through `FACT_CONFIG`. Either way they start without the venv activation `fact dev` may
    have inherited, so the workspaces they create see a plain environment.
    """
    python = python_argv(pinned)
    env = {CONFIG_VAR: str(config_file().resolve())} if pinned else {}
    path = deactivated_path(os.environ if environ is None else environ)
    if path is not None:
        env["PATH"] = path
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
    return [
        ChildSpec("temporal", temporal),
        ChildSpec("api", [*python, "-m", "fact.api"], env=env, unset=VENV_VARS),
        ChildSpec(
            "worker", [*python, "-m", "fact.worker"], wait_for=temporal_endpoint(settings), env=env, unset=VENV_VARS
        ),
    ]


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


def dev(
    *,
    pinned: Annotated[
        bool, typer.Option("--pinned", help="Run the API and worker from the version pinned with `fact use`.")
    ] = False,
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
    checkout = _pinned_or_exit(settings) if pinned else None
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if checkout is not None:
        typer.echo(f"Running pinned {checkout.name} from {checkout}")
    typer.echo(f"Temporal UI: http://localhost:{TEMPORAL_UI_PORT}  API: {settings.api_url}")
    code = run_all(build_commands(settings, temporal_exe, checkout))
    raise typer.Exit(0 if code == INTERRUPTED else code)
