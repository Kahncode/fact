"""
The task source interface: how fact reads work from a tracker and reports back to it.

Every caller (the scheduler, `fact run`, the worker's activities) goes through `TaskSource` and `source_for`, so it
does not care whether a task is a file or an issue. Keys carry their source: `task:M1-02` is a file task, and
`gh:owner/repo#12` a GitHub issue (`GitHubIssuesSource`).
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from fact.core.types import PRRef, Task

if TYPE_CHECKING:
    from fact.core.github.app import GitHubApp
    from fact.core.project_config import ProjectConfig
    from fact.core.settings import Settings

FILE_PREFIX = "task:"
GITHUB_PREFIX = "gh:"
_FILE_KEY_RE = re.compile(r"^task:([A-Za-z0-9][A-Za-z0-9._-]*)$")
_GITHUB_KEY_RE = re.compile(r"^gh:([A-Za-z0-9-]+/(?!\.{1,2}#)[A-Za-z0-9._-]+)#([1-9]\d*)$")

SourceKind = Literal["task", "gh"]


@dataclass(frozen=True)
class TaskKey:
    """A parsed task key: `kind` is its prefix, `id` the task id (`M1-02`) or issue number (`12`)."""

    kind: SourceKind
    id: str
    repo: str | None = None  # for `gh`: the repository holding the issue, "owner/repo"

    @property
    def number(self) -> int | None:
        return int(self.id) if self.kind == "gh" else None


@dataclass(frozen=True)
class ReadyItem:
    """
    A task marked ready, with its dependency depth (shallower tasks start first) and what holds it back, if anything.

    The scheduler may start it only when `waiting_on` is empty and `gated_by` is None.
    """

    task: Task
    depth: int = 0
    waiting_on: tuple[str, ...] = ()  # keys of the dependencies not done
    gated_by: str | None = None  # key of the nearest ancestor (parent issue) not marked ready


def parse_key(key: str) -> TaskKey:
    """Parse `task:M1-02` or `gh:owner/repo#12`; anything else raises ValueError."""
    if m := _FILE_KEY_RE.fullmatch(key):
        return TaskKey(kind="task", id=m.group(1))
    if m := _GITHUB_KEY_RE.fullmatch(key):
        return TaskKey(kind="gh", id=m.group(2), repo=m.group(1))
    msg = f"invalid task key {key!r}: use task:<id> or gh:<owner>/<repo>#<number>"
    raise ValueError(msg)


def is_issue(key: str) -> bool:
    """Whether `key` names a GitHub issue, which carries labels (a file task has none)."""
    return key.startswith(GITHUB_PREFIX)


def branch_name(key: str) -> str:
    """`task:M1-02` becomes `fact/task-m1-02`; `gh:owner/repo#12` becomes `fact/gh-12`."""
    parsed = parse_key(key)
    return f"fact/{parsed.kind}-{parsed.id.lower()}"


def dependency_key(task: Task, dep: str) -> str:
    """Key of one of `task`'s dependencies: a bare id (`M1-01`) belongs to the task's own source."""
    if ":" in dep:
        return dep
    parsed = parse_key(task.key)
    return f"{FILE_PREFIX}{dep}" if parsed.kind == "task" else f"{GITHUB_PREFIX}{parsed.repo}#{dep.lstrip('#')}"


class TaskSource(Protocol):
    """
    Where a project's tasks live. Reading methods see the source's current state; write-back methods report a run.

    A source whose state lives elsewhere (a file task's lives in its file and in fact's DB) may only log the writes.
    """

    async def list_ready(self, *, include_waiting: bool = False, close_parents: bool = False) -> list[ReadyItem]:
        """
        Tasks marked ready, not gated by an ancestor, whose dependencies are all done.

        `include_waiting`: also the others (`waiting_on`, `gated_by`). `close_parents`: close the ready parents without a
        work type with nothing left to wait on (only the scheduler's tick asks; such parents are never listed).
        """
        ...

    async def not_ready_ancestor(self, key: str) -> str | None:
        """Key of the nearest ancestor of task `key` not marked ready (its gate); None when there is none."""
        ...

    async def get(self, key: str) -> Task | None:
        """Return the task with this key, or None when the source has no such task."""
        ...

    async def deps_done(self, task: Task) -> bool:
        """Whether every task in `task.depends_on` is done."""
        ...

    async def is_closed(self, key: str) -> bool:
        """Whether the task was closed in its source (done or dropped); an unknown task is closed too."""
        ...

    async def comment(self, key: str, text: str) -> None: ...

    async def set_labels(self, key: str, add: list[str], remove: list[str]) -> None: ...

    async def write_context(self, key: str, section: str) -> None:
        """Replace the factory's own section of the task (never the person's text) with `section`."""
        ...

    async def link_prs(self, key: str, prs: list[PRRef]) -> None: ...

    async def close(self, key: str) -> None: ...

    def mark_done(self, worktree: Path, key: str) -> str | None:
        """
        Record the task's completion inside the change itself, in `worktree`; return the path it changed.

        None: this source records completion elsewhere (an issue is closed when its change set merges).
        """
        ...


def source_for(
    config: ProjectConfig, settings: Settings, root: Path | None = None, *, app: GitHubApp | None = None
) -> TaskSource:
    """
    Return the task source the project's `.fact/project.yaml` names.

    `root` is the checkout holding a `files` source's folder, such as a snapshot of the default branch. A `github`
    source uses `app`, else the process's shared App (`GitHubAppConfigError` while it is not configured).
    """
    source = config.task_source
    if source.kind == "files":
        if root is None:
            msg = "a files task source needs the checkout that holds its folder"
            raise ValueError(msg)
        from fact.core.tasks.files import FileTaskSource  # noqa: PLC0415 - files imports this module

        return FileTaskSource(root, source.path)
    from fact.core.github.app import shared_app  # noqa: PLC0415 - keep GitHub out of the files path
    from fact.core.tasks.github_issues import GitHubIssuesSource  # noqa: PLC0415 - it imports this module

    assert source.repo is not None  # noqa: S101 - TaskSourceConfig requires it for github
    return GitHubIssuesSource(source.repo, app or shared_app(settings))
