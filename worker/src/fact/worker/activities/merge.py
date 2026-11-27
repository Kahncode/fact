"""
`merge_change_set`: merge a run's PRs through the GitHub App, each only at the head commit CI passed on.

Before merging a PR it checks that the PR's head is still the commit the run pushed and CI passed on, and that every
check run and commit status there is complete and passing; if not, it merges nothing more and says `refused`. The merge
itself names that commit too (`sha`), so a push in between is refused by GitHub.

PRs are merged in order, skipping any already merged (a retried attempt, or an earlier round after the base moved on);
the first one GitHub refuses stops the rest. The activity then reads that PR's `mergeable_state` to say why:
`out_of_date` when it conflicts with or is behind its base (`dirty`, `behind`), which the workflow fixes by merging the
base, else `refused` (branch protection, head moved on), so the workflow falls back to waiting for a person. A
transient GitHub error is retried by Temporal; bad credentials or a missing PR or App fail without a retry.
"""

from typing import Any

import httpx
from fact.core.activity_contract import MERGE_CHANGE_SET, MergeChangeSetInput, MergeResult
from fact.core.github import GitHubApp, GitHubError
from fact.core.logging import bind_run, get_logger
from fact.core.types import PRRef
from fact.worker.ci_checks import list_checks
from fact.worker.pull_requests import OUT_OF_DATE_STATES, merge_state
from temporalio import activity
from temporalio.exceptions import ApplicationError

log = get_logger(__name__)

REFUSED_STATUSES = frozenset({405, 409, 422})  # not mergeable or blocked, head SHA moved on, merge not allowed
PERMANENT_STATUSES = frozenset({401, 404})  # bad credentials, PR or App not found


async def _not_green(client: httpx.AsyncClient, pr: PRRef, current: dict[str, Any]) -> str | None:
    """Why the PR must not merge at `pr.head_sha`: its head moved on since CI, or CI there is not all green; else None."""
    head = (current.get("head") or {}).get("sha")
    if head != pr.head_sha:
        return f"head is {head}, not the commit CI passed on"
    checks = await list_checks(client, pr.repo, pr.head_sha)
    if bad := [c.name for c in checks if not c.done or c.failed]:
        return "CI not green: " + ", ".join(bad)
    return None


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
                current = (await client.get(f"/repos/{pr.repo}/pulls/{pr.number}")).json()
                if current.get("merged"):
                    log.info("pr_already_merged", pr=pr.url)  # a retried attempt, or an earlier round of the run
                    return "merged"
                if (unsafe := await _not_green(client, pr, current)) is not None:
                    log.warning("merge_skipped", pr=pr.url, sha=pr.head_sha, reason=unsafe)
                    return "refused"
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
