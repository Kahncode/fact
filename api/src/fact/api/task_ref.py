"""
Open a project's task source; a `files` source reads the files as on `origin/<default branch>`, not the working tree.

A `github` source reads the issues through the GitHub App; its failures surface as `TaskReadError`.
"""

import asyncio
import io
import os
import subprocess
import tarfile
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from fact.core.github import GitHubError
from fact.core.logging import get_logger
from fact.core.project_config import ProjectConfig, load_project_config
from fact.core.settings import ProjectRef, Settings, project_path
from fact.core.tasks import TaskSource, dependency_key, source_for
from fact.core.types import Task

log = get_logger(__name__)

GIT_TIMEOUT_S = 60
# The API never waits on a credential prompt: a fetch that needs one fails and the last fetched state is used.
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}


class TaskReadError(RuntimeError):
    """The task source could not be read: git failed on the task files, or GitHub on the issues."""


@dataclass(frozen=True)
class TaskCheck:
    """A task as it is on the default branch, with the dependencies that are not done yet and its gate, if any."""

    task: Task
    missing_deps: list[str]
    gated_by: str | None = None  # the nearest ancestor not marked ready
    closed: bool = False  # closed in its source (`TaskSource.is_closed`): no run starts


def _git(repo: Path, *args: str) -> bytes:
    try:
        done = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=repo,
            env={**os.environ, **GIT_ENV},
            capture_output=True,
            check=True,
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        stderr = getattr(exc, "stderr", b"") or b""
        msg = f"git {' '.join(args)} failed in {repo}: {stderr.decode(errors='replace').strip() or exc}"
        raise TaskReadError(msg) from exc
    return done.stdout


def fetch(repo: Path, branch: str) -> None:
    """Update `origin/<branch>`; offline, fall back to the last fetched state."""
    try:
        _git(repo, "fetch", "--quiet", "origin", branch)
    except TaskReadError as exc:
        log.warning("task_fetch_failed", repo=str(repo), branch=branch, error=str(exc))


def _extract(repo: Path, ref: str, folder: str, dest: Path) -> None:
    """Write `<ref>:<folder>` under `dest`; a ref without the folder leaves `dest` empty."""
    listing = _git(repo, "ls-tree", "--name-only", ref, "--", folder).strip()
    if not listing:
        return
    archive = _git(repo, "archive", "--format=tar", ref, "--", folder)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(dest, filter="data")


def default_branch(ref: ProjectRef, config: ProjectConfig) -> str:
    home = next((r for r in config.repositories if r.full_name.lower() == ref.home.lower()), config.repositories[0])
    return home.default_branch


def _snapshot(repo: Path, branch: str, folder: str, dest: Path) -> None:
    fetch(repo, branch)
    _extract(repo, f"origin/{branch}", folder, dest)


@dataclass(frozen=True)
class ProjectTasks:
    """A project's config and its task source, as on its default branch."""

    config: ProjectConfig
    source: TaskSource


@asynccontextmanager
async def project_tasks(settings: Settings, ref: ProjectRef) -> AsyncIterator[ProjectTasks | None]:
    """
    Open the project's task source; None without a local checkout.

    A `files` source reads a snapshot of `origin/<default branch>`, fetched first (offline: the last fetched state),
    so it sees the tasks as merged, not as the working tree has them. The snapshot lasts as long as the context.
    GitHub errors (App not configured, API refusals, network), here or inside the context, raise `TaskReadError`.
    """
    path = project_path(ref)
    if path is None:
        yield None
        return
    config = await asyncio.to_thread(load_project_config, path)
    with tempfile.TemporaryDirectory(prefix="fact-tasks-") as tmp:
        root = Path(tmp)
        if config.task_source.kind == "files":  # git is blocking
            await asyncio.to_thread(_snapshot, path, default_branch(ref, config), config.task_source.path, root)
        try:
            yield ProjectTasks(config, source_for(config, settings, root))
        except (GitHubError, httpx.HTTPError) as exc:
            msg = f"cannot read the GitHub issues of project {ref.name}: {exc}"
            raise TaskReadError(msg) from exc


async def check_task(source: TaskSource, key: str) -> TaskCheck | None:
    """
    Read task `key` with its unfinished dependencies, ancestor not ready and closed state; None for no such task.

    Read afresh on every start, never from a tick's listing, which may be older than the issue's close.
    """
    task = await source.get(key)
    if task is None:
        return None
    missing = [
        dependency_key(task, d)
        for d in task.depends_on
        if not await source.deps_done(task.model_copy(update={"depends_on": [d]}))
    ]
    gated_by = await source.not_ready_ancestor(task.key)
    return TaskCheck(task=task, missing_deps=missing, gated_by=gated_by, closed=await source.is_closed(task.key))
