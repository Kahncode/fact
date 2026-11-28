"""
Git in a run's clone, always through `Runner.exec`: on the host with `LocalRunner`, in the container with `DockerRunner`.

Every workspace step that runs git (`sync_base`, `mark_task_done`, `classify_signals`, `push_branch`) goes through
here, so no activity opens a path of `WorkspaceHandle.repos` itself. The runner adds what its pool gives every command
(the `docker` pool: a fresh read token); a caller's `env` wins.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from fact.core.types import WorkspaceHandle
from fact.worker.runners.base import ExecExit, ExecLine, Runner, RunnerError

GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true", "GIT_MERGE_AUTOEDIT": "no"}
GIT_TIMEOUT_S = 600.0


@dataclass(frozen=True)
class WorkspaceGit:
    """`git` in one clone of a run's workspace; `worktree` is its path in `handle.repos`."""

    runner: Runner
    handle: WorkspaceHandle
    worktree: str

    async def __call__(
        self, *args: str, env: Mapping[str, str] | None = None, redact: Callable[[str], str] = str
    ) -> tuple[int, list[str]]:
        """Run `git <args>`; return its exit code and its merged output lines, each passed through `redact`."""
        lines: list[str] = []
        code = -1
        stream = self.runner.exec(
            self.handle, ["git", *args], cwd=self.worktree, env={**GIT_ENV, **(env or {})}, timeout=GIT_TIMEOUT_S
        )
        async for item in stream:
            if isinstance(item, ExecLine):
                lines.append(redact(item.text))
            elif isinstance(item, ExecExit):
                code = item.exit_code
        return code, lines

    async def ok(self, *args: str) -> bool:
        code, _ = await self(*args)
        return code == 0

    async def value(self, *args: str) -> str:
        """Return the first output line of `git <args>`, stripped; `RunnerError` unless it exits 0."""
        code, lines = await self(*args)
        if code != 0:
            msg = f"git {' '.join(args)} failed ({code}) in {self.worktree}: {' '.join(lines)[-500:]}"
            raise RunnerError(msg)
        return lines[0].strip() if lines else ""
