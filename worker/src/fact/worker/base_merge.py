"""
Merge a repository's default branch into the run's branch, in its worktree, through the runner (`sync_base`).

Only called when GitHub says the PR must take the base (it conflicts, or branch protection wants it up to date), never
because the base moved on: a PR that squashes cleanly is left alone.

The branch is squash-merged in the end, so a merge commit costs nothing and needs no force push. A conflicting merge
is left in progress, conflict markers and all, for an agent session to resolve; the next call concludes it once no
file is unmerged, or reports what is still unmerged.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from fact.core.types import WorkspaceHandle
from fact.worker.runners.base import Runner, RunnerError
from fact.worker.workspace_git import WorkspaceGit

BaseState = Literal["up_to_date", "merged", "conflicted"]


@dataclass(frozen=True)
class BaseMerge:
    state: BaseState
    conflicts: tuple[str, ...] = ()  # unmerged paths, relative to the worktree


async def _unmerged(git: WorkspaceGit) -> tuple[str, ...]:
    code, lines = await git("diff", "--name-only", "--diff-filter=U")
    if code != 0:
        msg = f"git diff --diff-filter=U failed ({code}) in {git.worktree}: {' '.join(lines)[-500:]}"
        raise RunnerError(msg)
    return tuple(line.strip() for line in lines if line.strip())


async def merge_base(  # noqa: PLR0913 - the worktree, its base and the mode
    runner: Runner,
    handle: WorkspaceHandle,
    worktree: str,
    default_branch: str,
    log: Callable[[str], None],
    *,
    verify_only: bool = False,
) -> BaseMerge:
    """
    Merge `origin/<default_branch>` (fetched by the caller) into the branch checked out in `worktree`.

    `up_to_date` if the branch already contains it, `merged` after a clean merge or after concluding one whose
    conflicts were resolved, `conflicted` with the unmerged paths otherwise. With `verify_only` (after a resolve
    session), it only concludes a merge left in progress and never starts one: `up_to_date` when there is none, even if
    the base moved on meanwhile. `RunnerError` if git fails another way.
    """
    git = WorkspaceGit(runner, handle, worktree)
    base = f"origin/{default_branch}"
    if await git.ok("rev-parse", "--quiet", "--verify", "MERGE_HEAD"):  # an earlier merge is still in progress
        if conflicts := await _unmerged(git):
            return BaseMerge("conflicted", conflicts)
        await _conclude(git, log)  # never chases a base that moved on meanwhile: GitHub says if that conflicts too
        return BaseMerge("merged")
    if verify_only or await git.ok("merge-base", "--is-ancestor", base, "HEAD"):
        return BaseMerge("up_to_date")
    log(f"$ git merge --no-edit {base}")
    code, lines = await git("merge", "--no-edit", base)
    for line in lines:
        log(line)
    if code == 0:
        return BaseMerge("merged")
    if conflicts := await _unmerged(git):
        return BaseMerge("conflicted", conflicts)
    msg = f"git merge {base} failed ({code}) in {worktree}: {' '.join(lines)[-1000:]}"
    raise RunnerError(msg)


async def _conclude(git: WorkspaceGit, log: Callable[[str], None]) -> None:
    """Commit a merge whose conflicts are all resolved, as the agent may have left it staged but uncommitted."""
    log("$ git commit --no-edit (concluding the merge)")
    code, lines = await git("commit", "--no-edit")
    for line in lines:
        log(line)
    if code != 0:
        msg = f"concluding the merge failed ({code}) in {git.worktree}: {' '.join(lines)[-1000:]}"
        raise RunnerError(msg)
