"""
`merge_change_set`: merge a run's PRs through the GitHub App, each only at the head commit CI passed on.

PRs are merged in order, skipping any already merged (a retried attempt, or an earlier round after the base moved on);
the first one GitHub refuses stops the rest. The activity then reads that PR's `mergeable_state` to say why:
`out_of_date` when it conflicts with or is behind its base (`dirty`, `behind`), which the workflow fixes by merging the
base, else `refused` (branch protection, head moved on), so the workflow falls back to waiting for a person. A
transient GitHub error is retried by Temporal; bad credentials or a missing PR or App fail without a retry.
"""

import httpx
from fact.core.activity_contract import MERGE_CHANGE_SET, MergeChangeSetInput, MergeResult
from fact.core.github import GitHubApp, GitHubError
from fact.core.logging import bind_run, get_logger
from fact.core.types import PRRef
from fact.worker.pull_requests import OUT_OF_DATE_STATES, merge_state
from temporalio import activity
from temporalio.exceptions import ApplicationError

log = get_logger(__name__)

REFUSED_STATUSES = frozenset({405, 409, 422})  # not mergeable or blocked, head SHA moved on, merge not allowed
PERMANENT_STATUSES = frozenset({401, 404})  # bad credentials, PR or App not found


class MergeActivities:
    """Control zone: merges PRs with the GitHub App."""

    def __init__(self, github: GitHubApp | None, merge_state_delay_s: float = 2.0) -> None:
        self._github = github
        self._delay_s = merge_state_delay_s

    @activity.defn(name=MERGE_CHANGE_SET)
    async def merge_change_set(self, spec: MergeChangeSetInput) -> MergeResult:
        """Merge every PR of `spec.prs` with `spec.method`; at the first refusal, say why and stop."""
        github = self._github
        if github is None:
            msg = "GitHub App not configured: set FACT_GITHUB_APP_ID and FACT_GITHUB_APP_PRIVATE_KEY_PATH"
            raise ApplicationError(msg, non_retryable=True)
        with bind_run(spec.run_id):
            for pr in spec.prs:
                result = await self._merge(github, pr, spec.method)
                if result != "merged":
                    return result
        return "merged"

    async def _merge(self, github: GitHubApp, pr: PRRef, method: str) -> MergeResult:
        body = {"merge_method": method, "sha": pr.head_sha}
        try:
            token = await github.installation_token(pr.repo)
            async with github.api_client(token.token) as client:
                if (await client.get(f"/repos/{pr.repo}/pulls/{pr.number}")).json().get("merged"):
                    log.info("pr_already_merged", pr=pr.url)  # a retried attempt, or an earlier round of the run
                    return "merged"
                try:
                    await client.put(f"/repos/{pr.repo}/pulls/{pr.number}/merge", json=body)
                except GitHubError as exc:
                    if exc.status_code not in REFUSED_STATUSES:
                        raise
                    state = await merge_state(client, pr.repo, pr.number, pr.head_sha, delay_s=self._delay_s)
                    log.warning("merge_refused", pr=pr.url, error=str(exc), mergeable_state=state)
                    return "out_of_date" if state in OUT_OF_DATE_STATES else "refused"
        except GitHubError as exc:
            retryable = exc.status_code not in PERMANENT_STATUSES
            raise ApplicationError(str(exc), non_retryable=not retryable) from exc
        except httpx.HTTPError as exc:
            msg = f"merging {pr.url} failed: {type(exc).__name__}: {exc}"
            raise ApplicationError(msg) from exc
        log.info("pr_merged", pr=pr.url, method=method, sha=pr.head_sha)
        return "merged"
