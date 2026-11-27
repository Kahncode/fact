"""A repository's bare mirror: cloned and fetched once, shared by every worktree cut from it."""

import asyncio
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from fact.core.logging import get_logger
from fact.worker.runners.base import RunnerError
from fact.worker.runners.branches import Git, GitOk, free_branch
from fact.worker.runners.workspace_files import make_writable_and_retry

log = get_logger(__name__)

# git refusing `worktree add -B` on a branch another worktree holds (wording before and since git 2.42); the runner
# runs git with LC_ALL=C so the message is never localized
_CHECKED_OUT = re.compile(r"is already (checked out|used by worktree) at")


@dataclass(frozen=True)
class Mirror:
    """The bare mirror at `path`; every method expects the caller to hold its file lock (`mirror_lock`)."""

    git: Git
    git_ok: GitOk
    path: Path

    async def sync(self, url: str) -> None:
        """Clone the mirror from `url` if there is none, then fetch it."""
        if not (self.path / "HEAD").exists():
            await self._clone(url)
        await self._keep_bare_out_of_worktrees()
        await self.git("fetch", "--prune", "origin", cwd=self.path)
        await self.git_ok("remote", "set-head", "origin", "--auto", cwd=self.path)  # follow default-branch renames

    async def _clone(self, url: str) -> None:
        """Clone into a scratch folder and rename it into place, so a clone cut short is never taken for a mirror."""
        partial = self.path.with_name(f".{self.path.name}.clone")  # a leading dot: never a repository's name
        for stale in (partial, self.path):  # a clone cut short, or a mirror folder that holds no repository
            if stale.exists():
                log.warning("mirror_discarded", path=str(stale))
                await asyncio.to_thread(shutil.rmtree, stale, onexc=make_writable_and_retry)
        await self.git("clone", "--bare", url, str(partial), cwd=self.path.parent)
        # A bare clone maps no remote branches; keep them under origin/* so local branches are never overwritten.
        await self.git("config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*", cwd=partial)
        partial.rename(self.path)

    async def _keep_bare_out_of_worktrees(self) -> None:
        """
        Keep `core.bare = true` in the mirror's own `config.worktree`, never in the shared config.

        With `extensions.worktreeConfig`, a shared `core.bare` also applies to every worktree, so git refuses
        work-tree commands there (`add`, `commit`). This also repairs mirrors cloned before the fix.
        """
        await self.git("config", "extensions.worktreeConfig", "true", cwd=self.path)  # before any --worktree write
        if await self.git_ok("config", "--file", "config", "--get", "core.bare", cwd=self.path):
            await self.git("config", "--worktree", "core.bare", "true", cwd=self.path)
            await self.git("config", "--file", "config", "--unset", "core.bare", cwd=self.path)

    async def add_on_free_branch(self, wanted: str, worktree: Path, start: str) -> str:
        """Add the worktree on a free branch; a branch git finds checked out elsewhere means pick again."""
        refused: set[str] = set()
        while True:  # `free_branch` raises once every name is held or refused
            branch = await free_branch(self.git, self.git_ok, self.path, wanted, skip=refused)
            try:
                await self.git("worktree", "add", "--no-track", "-B", branch, str(worktree), start, cwd=self.path)
            except RunnerError as exc:
                if not _CHECKED_OUT.search(str(exc)):
                    raise
                log.warning("branch_checked_out_elsewhere", branch=branch)
                refused.add(branch)
            else:
                return branch
