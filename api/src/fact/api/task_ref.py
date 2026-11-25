"""Read a project's task files as they are on `origin/<default branch>`, not as the working tree has them."""

import io
import os
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from fact.core.logging import get_logger
from fact.core.tasks.files import KEY_PREFIX, FileTaskSource
from fact.core.types import Task

log = get_logger(__name__)

GIT_TIMEOUT_S = 60
# The API never waits on a credential prompt: a fetch that needs one fails and the last fetched state is used.
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}


class TaskReadError(RuntimeError):
    """The task files could not be read from git."""


@dataclass(frozen=True)
class TaskCheck:
    """A task as it is on the default branch, with the dependencies that are not done yet."""

    task: Task
    missing_deps: list[str]


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


def read_task(repo: Path, branch: str, folder: str, key: str) -> TaskCheck | None:
    """
    Read task `key` from `origin/<branch>` of the checkout at `repo`, and which of its dependencies are not done.

    Returns None when the task is not on that branch. Call `fetch` first to see the latest state.
    """
    with tempfile.TemporaryDirectory(prefix="fact-tasks-") as tmp:
        _extract(repo, f"origin/{branch}", folder, Path(tmp))
        source = FileTaskSource(Path(tmp), folder)
        task = source.get(key)
        if task is None:
            return None
        missing = [d for d in task.depends_on if not source.deps_done(task.model_copy(update={"depends_on": [d]}))]
        return TaskCheck(task=task, missing_deps=[KEY_PREFIX + d for d in missing])
