"""The runner interface: prepare a workspace, run commands in it, tear it down."""

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from fact.core.types import WorkspaceHandle, WorkspaceSpec


class RunnerError(RuntimeError):
    """A runner could not prepare, run or tear down a workspace."""


@dataclass(frozen=True)
class ExecLine:
    """One line of merged stdout and stderr."""

    text: str


@dataclass(frozen=True)
class ExecExit:
    """Always the last item of an `exec` stream. `exit_code` is -1 when the command timed out."""

    exit_code: int
    timed_out: bool = False


ExecOutput = ExecLine | ExecExit


@dataclass(frozen=True)
class RunnerCapabilities:
    name: str
    isolation: str  # "none" for local-worktree, "container" for docker
    runs_as_user: bool = True
    supports_resume: bool = True


class Runner(Protocol):
    async def prepare(self, spec: WorkspaceSpec) -> WorkspaceHandle:
        """Create the run's workspace: one checkout per repository on its task branch."""
        ...

    def exec(
        self,
        handle: WorkspaceHandle,
        argv: Sequence[str],
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[ExecOutput]:
        """Run `argv` (never a shell string) and stream its output lines, then one `ExecExit`."""
        ...

    async def teardown(self, handle: WorkspaceHandle) -> None:
        """Remove the workspace. Safe to call twice."""
        ...

    async def fetch(self, full_name: str) -> None:
        """Update the `origin/*` branches the repository's workspaces see, such as before merging the base."""
        ...

    def capabilities(self) -> RunnerCapabilities: ...
