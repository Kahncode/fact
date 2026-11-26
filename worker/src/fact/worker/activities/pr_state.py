"""
`wait_for_pr_closed`: wait until each of a run's PRs is merged or closed, and say which.

Polls every PR still open every `poll_interval_s`, heartbeating, for as long as review takes (the workflow gives it up
to 30 days). A transient GitHub error only skips a poll; a permanent one (bad credentials, PR or App not found) fails
the activity without a retry. The workflow records the wait as a gate-like step: this activity reports no step.
"""

import asyncio

import httpx
from fact.core.activity_contract import WAIT_FOR_PR_CLOSED, PRState, WaitForPRClosedInput
from fact.core.github import GitHubApp, GitHubError
from fact.core.logging import bind_run, get_logger
from fact.core.types import PRRef
from temporalio import activity
from temporalio.exceptions import ApplicationError

log = get_logger(__name__)

POLL_INTERVAL_S = 60.0
PERMANENT_STATUSES = frozenset({401, 404, 422})  # bad credentials, PR or App not found, bad request


def _heartbeat(states: dict[int, PRState]) -> None:
    if activity.in_activity():
        activity.heartbeat(len(states))


class PRStateActivities:
    """Control zone: reads PR state with the GitHub App."""

    def __init__(self, github: GitHubApp | None, poll_interval_s: float = POLL_INTERVAL_S) -> None:
        self._github = github
        self._poll_s = poll_interval_s

    @activity.defn(name=WAIT_FOR_PR_CLOSED)
    async def wait_for_pr_closed(self, spec: WaitForPRClosedInput) -> list[PRState]:
        """Return `merged` or `closed` for each PR of `spec.prs`, in order, once none is open."""
        github = self._github
        if github is None:
            msg = "GitHub App not configured: set FACT_GITHUB_APP_ID and FACT_GITHUB_APP_PRIVATE_KEY_PATH"
            raise ApplicationError(msg, non_retryable=True)
        states: dict[int, PRState] = {}  # index in `spec.prs` -> how it ended
        with bind_run(spec.run_id):
            while True:
                for i, pr in enumerate(spec.prs):
                    if i not in states and (state := await self._poll(github, pr)) is not None:
                        states[i] = state
                        log.info("pr_closed", pr=pr.url, state=state)
                if len(states) == len(spec.prs):
                    return [states[i] for i in range(len(spec.prs))]
                _heartbeat(states)
                await asyncio.sleep(self._poll_s)

    @staticmethod
    async def _poll(github: GitHubApp, pr: PRRef) -> PRState | None:
        """Return how the PR ended, or None while it is open or after a transient error."""
        try:
            token = await github.installation_token(pr.repo)  # cached until near expiry
            async with github.api_client(token.token) as client:
                body = (await client.get(f"/repos/{pr.repo}/pulls/{pr.number}")).json()
        except GitHubError as exc:
            if exc.status_code in PERMANENT_STATUSES:
                raise ApplicationError(str(exc), non_retryable=True) from exc
            log.warning("pr_poll_failed", pr=pr.url, error=str(exc))
            return None
        except httpx.HTTPError as exc:
            log.warning("pr_poll_failed", pr=pr.url, error=f"{type(exc).__name__}: {exc}")
            return None
        if body.get("merged"):
            return "merged"
        return "closed" if body.get("state") == "closed" else None
