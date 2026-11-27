"""`open_change_set`: push the run's branch and open or update its PR, as a `github` step in stage `open_pr`."""

from collections.abc import Callable
from pathlib import Path

from fact.core.activity_contract import OPEN_CHANGE_SET, OpenChangeSetInput
from fact.core.enums import StepKind, StepOutcome
from fact.core.github import GitHubApp, GitHubError
from fact.core.types import PRRef, WorkspaceHandle
from fact.worker.git_push import PushError, PushRefusedError, branch_sha, current_branch, push_branch
from fact.worker.pr_body import render_pr_body
from fact.worker.pull_requests import open_or_update_pr, repo_info
from fact.worker.reporting import StepContext, StepReporter
from fact.worker.runners.local_worktree import github_url
from temporalio import activity
from temporalio.exceptions import ApplicationError

OPEN_PR_STAGE = "open_pr"
PERMANENT_STATUSES = frozenset({401, 404, 422})  # bad credentials, App not installed, PR rejected (no commits)


def _non_retryable(message: str) -> ApplicationError:
    return ApplicationError(message, non_retryable=True)


def _single_repo(handle: WorkspaceHandle) -> tuple[str, Path]:
    if len(handle.repos) != 1:
        msg = f"open_change_set supports one repository in v0, the workspace has {len(handle.repos)}"
        raise _non_retryable(msg)
    ((full_name, path),) = handle.repos.items()
    return full_name, Path(path)


async def _check_checkout(worktree: Path, branch: str) -> None:
    """Require the run's branch checked out; anything else is a bug or a tampered worktree, not worth a retry."""
    try:
        checked_out = await current_branch(worktree)
    except PushError as exc:
        msg = f"the worktree has no branch checked out (detached HEAD?); expected {branch!r}"
        raise _non_retryable(msg) from exc
    if checked_out != branch:
        msg = f"the worktree is on {checked_out!r}, not the run's branch {branch!r}"
        raise _non_retryable(msg)


class ChangeSetActivities:
    """Control zone only: holds the GitHub App. `remote_url` maps `owner/repo` to the push URL (tests use a bare repo)."""

    def __init__(
        self, github: GitHubApp | None, reporter: StepReporter, remote_url: Callable[[str], str] = github_url
    ) -> None:
        self._github = github
        self._reporter = reporter
        self._remote_url = remote_url

    @activity.defn(name=OPEN_CHANGE_SET)
    async def open_change_set(self, spec: OpenChangeSetInput) -> list[PRRef]:
        async with self._reporter.step(spec.run_id, OPEN_PR_STAGE, StepKind.GITHUB, spec.attempt) as step:
            try:
                pr = await self._open(spec, step)
            except PushRefusedError as exc:
                raise _non_retryable(str(exc)) from exc
            except GitHubError as exc:
                if exc.status_code in PERMANENT_STATUSES:
                    raise _non_retryable(str(exc)) from exc
                raise
            step.finish(StepOutcome.PASSED, pr.url, outputs={"prs": [pr.model_dump()]})
        return [pr]

    async def _open(self, spec: OpenChangeSetInput, step: StepContext) -> PRRef:
        if self._github is None:
            msg = "GitHub App not configured: set FACT_GITHUB_APP_ID and FACT_GITHUB_APP_PRIVATE_KEY_PATH"
            raise _non_retryable(msg)
        full_name, worktree = _single_repo(spec.handle)
        branch = spec.branch
        await _check_checkout(worktree, branch)
        token = await self._github.installation_token(full_name)
        async with self._github.api_client(token.token) as client:
            repo = await repo_info(client, full_name)
            await push_branch(
                worktree,
                self._remote_url(full_name),
                branch,
                default_branch=repo.default_branch,
                token=token.token,
                log=step.log,
            )
            sha = await branch_sha(worktree, branch)
            body = render_pr_body(
                summary=spec.summary,
                task=spec.task,
                repo_url=repo.html_url,
                default_branch=repo.default_branch,
                check_results=spec.check_results,
                run_url=spec.run_url,
                criteria=spec.criteria,
                pr_count=1,  # single-repo in v0: one PR is the whole change set
            )
            pr = await open_or_update_pr(client, repo, branch=branch, title=spec.title, body=body)
        step.log(f"{'opened' if pr.created else 'updated'} PR #{pr.number}: {pr.url}")
        return PRRef(repo=full_name, number=pr.number, url=pr.url, head_sha=sha)
