"""
`push_branch`: push the run's branch from its clone, as a `shell` step in stage `push`, in the `workspace` zone.

The commands run through `Runner.exec` (`git_push.push_branch`), so with the `docker` pool the push happens inside the
run's container. It is the only command of a run given a `contents: write` installation token, limited to the run's
repositories, in its environment alone. `open_change_set` then opens or updates the PR from what GitHub holds.
"""

from fact.core.activity_contract import PUSH_BRANCH, PushBranchInput
from fact.core.enums import StepKind, StepOutcome
from fact.core.github import GitHubApp
from fact.worker.git_push import PushRefusedError, push_branch
from fact.worker.reporting import StepReporter
from fact.worker.runners.base import Runner
from fact.worker.workspace_git import WorkspaceGit
from temporalio import activity
from temporalio.exceptions import ApplicationError

PUSH_STAGE = "push"
WRITE_PERMISSIONS = {"contents": "write"}


class PushActivities:
    """Without a `github` App the push gets no token: only a remote needing none (a local bare repository) takes it."""

    def __init__(self, runner: Runner, github: GitHubApp | None, reporter: StepReporter) -> None:
        self._runner = runner
        self._github = github
        self._reporter = reporter

    @activity.defn(name=PUSH_BRANCH)
    async def push_branch(self, spec: PushBranchInput) -> str:
        """Push `spec.branch`, LFS objects first and without pre-push hooks; return the commit pushed."""
        worktree = spec.handle.repos.get(spec.repo)
        if worktree is None:
            msg = f"repository {spec.repo!r} is not in the workspace"
            raise ApplicationError(msg, non_retryable=True)
        async with self._reporter.step(spec.run_id, PUSH_STAGE, StepKind.SHELL, spec.attempt) as step:
            git = WorkspaceGit(self._runner, spec.handle, worktree)
            try:
                sha = await push_branch(
                    git,
                    spec.branch,
                    default_branch=spec.default_branch,
                    token=await self._write_token(list(spec.handle.repos)),
                    log=step.log,
                )
            except PushRefusedError as exc:  # a bug or a tampered clone, not worth a retry
                raise ApplicationError(str(exc), non_retryable=True) from exc
            step.finish(StepOutcome.PASSED, f"pushed {spec.branch} at {sha[:12]}", outputs={"head_sha": sha})
        return sha

    async def _write_token(self, full_names: list[str]) -> str | None:
        if self._github is None:
            return None
        return (await self._github.repositories_token(full_names, WRITE_PERMISSIONS)).token
