"""
`merge_change_set`: merge a run's PRs through the GitHub App, each only at the head commit CI passed on.

PRs are merged in order; the first one GitHub refuses (not mergeable, branch protection, head moved on) stops the rest
and the activity returns False, so the workflow can fall back to waiting for a person. A transient GitHub error is
retried by Temporal; bad credentials or a missing PR or App fail without a retry.
"""

import httpx
from fact.core.activity_contract import MERGE_CHANGE_SET, MergeChangeSetInput
from fact.core.github import GitHubApp, GitHubError
from fact.core.logging import bind_run, get_logger
from fact.core.types import PRRef
from temporalio import activity
from temporalio.exceptions import ApplicationError

log = get_logger(__name__)

REFUSED_STATUSES = frozenset({405, 409, 422})  # not mergeable or blocked, head SHA moved on, merge not allowed
PERMANENT_STATUSES = frozenset({401, 404})  # bad credentials, PR or App not found


class MergeActivities:
    """Control zone: merges PRs with the GitHub App."""

    def __init__(self, github: GitHubApp | None) -> None:
        self._github = github

    @activity.defn(name=MERGE_CHANGE_SET)
    async def merge_change_set(self, spec: MergeChangeSetInput) -> bool:
        """Merge every PR of `spec.prs` with `spec.method`; False as soon as GitHub refuses one."""
        github = self._github
        if github is None:
            msg = "GitHub App not configured: set FACT_GITHUB_APP_ID and FACT_GITHUB_APP_PRIVATE_KEY_PATH"
            raise ApplicationError(msg, non_retryable=True)
        with bind_run(spec.run_id):
            for pr in spec.prs:
                if not await self._merge(github, pr, spec.method):
                    return False
        return True

    @staticmethod
    async def _merge(github: GitHubApp, pr: PRRef, method: str) -> bool:
        body = {"merge_method": method, "sha": pr.head_sha}
        try:
            token = await github.installation_token(pr.repo)
            async with github.api_client(token.token) as client:
                await client.put(f"/repos/{pr.repo}/pulls/{pr.number}/merge", json=body)
        except GitHubError as exc:
            if exc.status_code in REFUSED_STATUSES:
                log.warning("merge_refused", pr=pr.url, error=str(exc))
                return False
            retryable = exc.status_code not in PERMANENT_STATUSES
            raise ApplicationError(str(exc), non_retryable=not retryable) from exc
        except httpx.HTTPError as exc:
            msg = f"merging {pr.url} failed: {type(exc).__name__}: {exc}"
            raise ApplicationError(msg) from exc
        log.info("pr_merged", pr=pr.url, method=method, sha=pr.head_sha)
        return True
