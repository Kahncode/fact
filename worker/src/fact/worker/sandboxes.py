"""
Sandbox images: built from a project version's home checkout when the worker loads it, never by a run.

A sandbox is `.fact/sandboxes/<name>/` (a Docker build context with its `Dockerfile`), named by a workflow class
(`FactWorkflow.sandbox`) or by the pool's `default_sandbox`. Its image is tagged `fact-sandbox/<project>/<name>:<hash>`,
the hash covering the folder's file paths and contents, and labelled as fact's, so cleanup can recognise it. A tag that
already exists is not built again. The name-to-tag map lives in the worker process: every load computes it again, so
losing it costs only the `docker image inspect` calls.
"""

import hashlib
from collections.abc import Iterable, Mapping
from pathlib import Path

from fact.core.logging import get_logger
from fact.worker.runners.base import ExecExit, ExecLine
from fact.worker.runners.process import build_env, spawn, stream

log = get_logger(__name__)

SANDBOXES_SUBDIR = Path(".fact") / "sandboxes"
DOCKER = "docker"  # the CLI, found on PATH
INFO_TIMEOUT_S = 60.0
INSPECT_TIMEOUT_S = 60.0
BUILD_TIMEOUT_S = 3600.0
HASH_CHARS = 16
TAIL_LINES = 20  # of a failed build's output, in its error
LABEL_SANDBOX = "fact.sandbox"  # `true` on every sandbox image fact builds
LABEL_PROJECT = "fact.project"
LABEL_NAME = "fact.sandbox.name"


class SandboxError(RuntimeError):
    """A sandbox folder is missing or its image failed to build; the message names the project and the sandbox."""


class DockerUnavailableError(RuntimeError):
    """A project uses the `docker` pool but `docker info` fails: the worker cannot serve it, so it does not start."""


async def _docker(args: list[str], cwd: Path, timeout: float) -> tuple[int, list[str]]:  # noqa: ASYNC109
    """Run `docker <args>`; return its exit code and merged output lines. A missing CLI exits 127."""
    try:
        proc = await spawn([DOCKER, *args], cwd, build_env(None))
    except FileNotFoundError:
        return 127, [f"{DOCKER}: command not found"]
    lines: list[str] = []
    code = -1
    async for out in stream(proc, timeout):
        if isinstance(out, ExecLine):
            lines.append(out.text)
        elif isinstance(out, ExecExit):
            code = out.exit_code
    return code, lines


async def check_docker(cwd: Path) -> None:
    """Raise `DockerUnavailableError` unless `docker info` succeeds."""
    code, lines = await _docker(["info", "--format", "{{.ServerVersion}}"], cwd, INFO_TIMEOUT_S)
    if code != 0:
        detail = " ".join(lines[-TAIL_LINES:]).strip()
        msg = (
            f"Docker is not available (`docker info` exited {code}: {detail}); a project uses the docker runner pool, "
            "so start Docker (Docker Desktop on Windows and macOS) and start the worker again"
        )
        raise DockerUnavailableError(msg)


def folder_hash(folder: Path) -> str:
    """Hash every file's path (relative, `/`-separated) and contents, in path order."""
    digest = hashlib.sha256()
    files = sorted((p.relative_to(folder).as_posix(), p) for p in folder.rglob("*") if p.is_file())
    for rel, path in files:
        data = path.read_bytes()
        for part in (rel.encode(), str(len(data)).encode(), data):
            digest.update(part)
            digest.update(b"\0")
    return digest.hexdigest()[:HASH_CHARS]


def image_tag(project: str, name: str, folder: Path) -> str:
    return f"fact-sandbox/{project.lower()}/{name}:{folder_hash(folder)}"


async def _build(project: str, name: str, folder: Path, tag: str) -> None:
    labels = [f"{LABEL_SANDBOX}=true", f"{LABEL_PROJECT}={project}", f"{LABEL_NAME}={name}"]
    args = ["build", "--progress=plain", "--tag", tag]
    for label in labels:
        args += ["--label", label]
    log.info("sandbox_build_started", project=project, sandbox=name, tag=tag)
    proc = await spawn([DOCKER, *args, str(folder)], folder, build_env(None))
    tail: list[str] = []
    code = -1
    async for out in stream(proc, BUILD_TIMEOUT_S):
        if isinstance(out, ExecLine):
            log.info("sandbox_build_output", project=project, sandbox=name, line=out.text)
            tail = [*tail[-(TAIL_LINES - 1) :], out.text]
        elif isinstance(out, ExecExit):
            code = out.exit_code
    if code != 0:
        msg = f"project {project}: sandbox {name!r} failed to build (exit {code}): " + "\n".join(tail)
        raise SandboxError(msg)
    log.info("sandbox_built", project=project, sandbox=name, tag=tag)


async def build_sandboxes(project: str, checkout: Path, names: Iterable[str]) -> Mapping[str, str]:
    """
    Build each named sandbox of `checkout` whose tag does not exist yet; return each name's tag.

    Raise `DockerUnavailableError` when `docker info` fails, and `SandboxError` for a missing folder or failed build.
    """
    folders = {name: checkout / SANDBOXES_SUBDIR / name for name in sorted(set(names))}
    for name, folder in folders.items():
        if not folder.is_dir():
            msg = f"project {project}: sandbox {name!r} has no folder {SANDBOXES_SUBDIR.as_posix()}/{name}/"
            raise SandboxError(msg)
    if not folders:
        return {}
    await check_docker(checkout)
    tags: dict[str, str] = {}
    for name, folder in folders.items():
        tag = image_tag(project, name, folder)
        code, _ = await _docker(["image", "inspect", "--format", "{{.Id}}", tag], checkout, INSPECT_TIMEOUT_S)
        if code == 0:
            log.info("sandbox_reused", project=project, sandbox=name, tag=tag)
        else:
            await _build(project, name, folder, tag)
        tags[name] = tag
    return tags
