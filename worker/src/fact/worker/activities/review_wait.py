"""
`wait_for_review`: wait until something happens on one of a run's open PRs, and say what, for each PR it happened on.

Every `poll_interval_s` it reads each PR and its reviews; once a round finds an event (`fact.worker.reviews.review_event`:
merged, closed, approved on its head, or a review requesting changes the run has not handled, submitted since
`reviews_since`), it returns that round's
events, at most one per PR. Only maintainers' reviews count (`fact.worker.reviews.Maintainers`, looked up once per
login and activity call); each review or comment by anyone else since `reviews_since` gets one fixed reply
(`fact.worker.outsiders`) and is otherwise ignored. The workflow gives it what is left of the run's 30 days. A transient
GitHub error reading the PR only skips a poll; a permanent one (bad credentials, PR or App not found) fails the activity
without a retry. A permission lookup that fails (other than with a 404, which means not a maintainer) fails the
activity, which Temporal retries: the author is never counted by default. The workflow records the wait as a gate-like
step: this activity reports no step.
"""

import asyncio
from datetime import datetime
from typing import Any

import httpx
from fact.core.activity_contract import WAIT_FOR_REVIEW, ReviewEvent, WaitForReviewInput
from fact.core.github import GitHubApp, GitHubError
from fact.core.logging import bind_run, get_logger
from fact.core.types import PRRef
from fact.worker.outsiders import candidates, reply_once
from fact.worker.reviews import Maintainers, paged, pr_reviews, review_event
from temporalio import activity
from temporalio.exceptions import ApplicationError

log = get_logger(__name__)

POLL_INTERVAL_S = 60.0
PERMANENT_STATUSES = frozenset({401, 404, 422})  # bad credentials, PR or App not found, bad request
UNAUTHORIZED = 401
NO_APP = "GitHub App not configured: set FACT_GITHUB_APP_ID and FACT_GITHUB_APP_PRIVATE_KEY_PATH"


class ReviewWaitActivities:
    """Control zone: reads PRs and their reviews with the GitHub App."""

    def __init__(self, github: GitHubApp | None, poll_interval_s: float = POLL_INTERVAL_S) -> None:
        self._github = github
        self._poll_s = poll_interval_s

    @activity.defn(name=WAIT_FOR_REVIEW)
    async def wait_for_review(self, spec: WaitForReviewInput) -> list[ReviewEvent]:
        """Return the first round's events on `spec.prs`, in their order, once a round finds one."""
        github = self._github
        if github is None:
            raise ApplicationError(NO_APP, non_retryable=True)
        handled = frozenset(spec.handled_reviews)
        lookups = {repo: Maintainers(repo) for repo in dict.fromkeys(pr.repo for pr in spec.prs)}  # one per call
        with bind_run(spec.run_id):
            while True:
                events = [
                    e
                    for pr in spec.prs
                    if (e := await self._poll(github, pr, handled, spec.reviews_since, lookups[pr.repo])) is not None
                ]
                if events:
                    for event in events:
                        log.info("review_event", pr=event.pr.url, kind=event.kind, review=event.review_id)
                    return events
                await asyncio.sleep(self._poll_s)

    @staticmethod
    async def _poll(
        github: GitHubApp, pr: PRRef, handled: frozenset[int], since: datetime | None, lookup: Maintainers
    ) -> ReviewEvent | None:
        """Return the PR's event, or None while nothing happened or after a transient error."""
        try:
            token = await github.installation_token(pr.repo)  # cached until near expiry
            async with github.api_client(token.token) as client:
                body = (await client.get(f"/repos/{pr.repo}/pulls/{pr.number}")).json()
                if body.get("merged") or body.get("state") == "closed":
                    return review_event(pr, body, [], handled, maintainers=(), since=since)
                reviews = await pr_reviews(client, pr)
                inline = await paged(client, f"/repos/{pr.repo}/pulls/{pr.number}/comments")
                conversation = await paged(client, f"/repos/{pr.repo}/issues/{pr.number}/comments")
                read = (reviews, inline, conversation)
                return await _decide(client, pr, body, read, lookup, handled=handled, since=since)
        except GitHubError as exc:
            if exc.status_code in PERMANENT_STATUSES:
                raise ApplicationError(str(exc), non_retryable=True) from exc
            log.warning("pr_poll_failed", pr=pr.url, error=str(exc))
            return None
        except httpx.HTTPError as exc:
            log.warning("pr_poll_failed", pr=pr.url, error=f"{type(exc).__name__}: {exc}")
            return None


type Items = list[dict[str, Any]]


async def _decide(  # noqa: PLR0913 - the PR as read, and what the run already handled
    client: httpx.AsyncClient,
    pr: PRRef,
    body: dict[str, Any],
    read: tuple[Items, Items, Items],
    lookup: Maintainers,
    *,
    handled: frozenset[int],
    since: datetime | None,
) -> ReviewEvent | None:
    """Reply once to each outsider's action on the open PR, then return its event from its maintainers' reviews."""
    reviews, inline, conversation = read
    actions = candidates(reviews, inline, conversation, since)
    try:
        maintainers = await lookup.among(client, [a.login for a in actions])
    except (GitHubError, httpx.HTTPError) as exc:
        status = getattr(exc, "status_code", None)
        msg = f"permission lookup failed, no author counted: {type(exc).__name__}: {exc}"
        raise ApplicationError(msg, non_retryable=status == UNAUTHORIZED) from exc
    posted = "\n".join(str(c.get("body") or "") for c in [*inline, *conversation])
    await reply_once(client, pr.repo, pr.number, [a for a in actions if a.login not in maintainers], posted)
    return review_event(pr, body, reviews, handled, maintainers=maintainers, since=since)
