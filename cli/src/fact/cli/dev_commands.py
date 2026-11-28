"""
The processes `fact dev` runs: Temporal, the API and the workers, each as a `ChildSpec`.

Every factory process starts without the venv activation `fact dev` may have inherited, so the workspaces they create
see a plain environment.
"""

import os
import sys
from collections.abc import Mapping
from pathlib import Path

import typer
from fact.cli.generations import worker_name
from fact.cli.processes import SOURCE_BUILD, ChildSpec
from fact.cli.use import venv_python
from fact.core.generations import Generation
from fact.core.settings import Settings

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


def plain_env(environ: Mapping[str, str]) -> dict[str, str]:
    """Return `environ` without a venv activation, for the `fact dev` a launcher runs from a pin."""
    env = {k: v for k, v in environ.items() if k not in VENV_VARS}
    if (path := deactivated_path(environ)) is not None:
        env["PATH"] = path
    return env
