"""`fact dev`: Temporal dev server, API and worker in one command."""

import shutil
import sys
from pathlib import Path
from typing import Annotated

import typer
from fact.cli.supervisor import INTERRUPTED, ChildSpec, run_all
from fact.cli.use import PinError, current_pin
from fact.core.settings import CONFIG_VAR, Settings, config_file, get_settings

TEMPORAL_UI_PORT = 8233


def temporal_endpoint(settings: Settings) -> tuple[str, int]:
    """Split `temporal.address` (`host:port`, IPv6 hosts in brackets) for the port check."""
    address = settings.temporal.address
    host, _, port = address.rpartition(":")
    if not port.isdigit():
        msg = f"temporal.address must be host:port, got {address!r}"
        raise typer.BadParameter(msg)
    return host.strip("[]") or "localhost", int(port)


def python_argv(pinned: Path | None, uv: str | None) -> list[str]:
    """Return the Python launcher: this interpreter, or `uv run` in the pinned checkout."""
    if pinned is None:
        return [sys.executable]
    if uv is None:
        msg = "uv is required to run the pinned version"
        raise ValueError(msg)
    return [uv, "run", "--frozen", "--project", str(pinned), "python"]


def build_commands(
    settings: Settings, temporal_exe: str, pinned: Path | None = None, uv: str | None = None
) -> list[ChildSpec]:
    """
    Return the processes `fact dev` starts, in order; the worker waits for Temporal's port.

    With `pinned`, the API and worker run from that checkout, reading this tree's `fact.yaml` and `.env` (which a
    pinned checkout lacks) through `FACT_CONFIG`.
    """
    python = python_argv(pinned, uv)
    env = {CONFIG_VAR: str(config_file().resolve())} if pinned else {}
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
        ChildSpec("api", [*python, "-m", "fact.api"], env=env),
        ChildSpec("worker", [*python, "-m", "fact.worker"], wait_for=temporal_endpoint(settings), env=env),
    ]


def _pinned_or_exit(settings: Settings) -> tuple[Path, str]:
    uv = shutil.which("uv")
    if uv is None:
        typer.echo("error: `uv` is not on PATH", err=True)
        raise typer.Exit(1)
    try:
        return current_pin(settings), uv
    except PinError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


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
    checkout, uv = _pinned_or_exit(settings) if pinned else (None, None)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if checkout is not None:
        typer.echo(f"Running pinned {checkout.name} from {checkout}")
    typer.echo(f"Temporal UI: http://localhost:{TEMPORAL_UI_PORT}  API: {settings.api_url}")
    code = run_all(build_commands(settings, temporal_exe, checkout, uv))
    raise typer.Exit(0 if code == INTERRUPTED else code)
