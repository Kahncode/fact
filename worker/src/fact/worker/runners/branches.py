"""Pick a free task branch in a mirror: git checks a branch out in one worktree at a time."""

from collections.abc import Awaitable, Callable
from pathlib import Path

from fact.worker.runners.base import RunnerError

MAX_SUFFIX = 50
HEAD_PREFIX = "branch refs/heads/"

GitOk = Callable[..., Awaitable[bool]]
Git = Callable[..., Awaitable[str]]


async def held_branches(git: Git, mirror: Path) -> set[str]:
    """Branches checked out in a worktree of `mirror`, after pruning worktrees whose folder is gone."""
    await git("worktree", "prune", cwd=mirror)
    listing = await git("worktree", "list", "--porcelain", cwd=mirror)
    return {line.removeprefix(HEAD_PREFIX) for line in listing.splitlines() if line.startswith(HEAD_PREFIX)}


async def free_branch(git: Git, git_ok: GitOk, mirror: Path, wanted: str) -> str:
    """
    Return `wanted`, or `<wanted>-2`, `-3`, ... when another worktree holds it.

    A suffixed name is also skipped when it exists on the remote, so a run never takes over another run's PR branch;
    `wanted` itself may exist there, which is how a task branch is resumed.
    """
    held = await held_branches(git, mirror)
    for n in range(1, MAX_SUFFIX + 1):
        name = wanted if n == 1 else f"{wanted}-{n}"
        if name in held:
            continue
        if n > 1 and await git_ok("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{name}", cwd=mirror):
            continue
        return name
    msg = f"no free branch name for {wanted!r}: {MAX_SUFFIX} worktrees or remote branches hold its variants"
    raise RunnerError(msg)
