"""
`wait_for_answer`: wait until a maintainer answers a run's question on its issue, by a comment mentioning the App.

Every `poll_interval_s` it reads the issue's comments. Only comments by a person (no bot, fact's own question among
them), made since the question was asked, not handled yet and mentioning the App (`@<slug>`, case-insensitive) count;
any other comment is ignored and wakes nothing. Of those, a comment by anyone without write access on the repository
(`fact.worker.reviews.Maintainers`) gets one fixed reply (`fact.worker.outsiders`) and is otherwise ignored; once a poll
finds a maintainer's, it returns them, oldest first. A transient GitHub error only skips a poll; a permanent one (bad
credentials, issue or App not found) fails the activity without a retry. A permission lookup that fails (other than
with a 404) fails the attempt, which Temporal retries: the author is never counted by default.
"""

import asyncio
from datetime import datetime

import httpx
from fact.core.activity_contract import WAIT_FOR_ANSWER, Comment, WaitForAnswerInput
from fact.core.github import GitHubApp, GitHubError
from fact.core.logging import bind_run, get_logger
from fact.worker.outsiders import Action, reply_once
from fact.worker.reviews import Maintainers, by_person, login_of, mentions, paged
from temporalio import activity
from temporalio.exceptions import ApplicationError

log = get_logger(__name__)

POLL_INTERVAL_S = 60.0
PERMANENT_STATUSES = frozenset({401, 404, 422})
UNAUTHORIZED = 401
NO_APP = "GitHub App not configured: set FACT_GITHUB_APP_ID and FACT_GITHUB_APP_PRIVATE_KEY_PATH"


def _comment(item: dict) -> Comment:
    return Comment(
        id=item["id"],
        kind="comment",
        author=login_of(item),
        body=str(item.get("body") or ""),
        created_at=datetime.fromisoformat(str(item["created_at"])),
        url=str(item.get("html_url") or ""),
    )


class AnswerWaitActivities:
    """Control zone: reads an issue's comments with the GitHub App."""

    def __init__(self, github: GitHubApp | None, poll_interval_s: float = POLL_INTERVAL_S) -> None:
        self._github = github
        self._poll_s = poll_interval_s

    @activity.defn(name=WAIT_FOR_ANSWER)
    async def wait_for_answer(self, spec: WaitForAnswerInput) -> list[Comment]:
        """Return the maintainers' new comments mentioning the App, once a poll finds one."""
        github = self._github
        if github is None:
            raise ApplicationError(NO_APP, non_retryable=True)
        lookup = Maintainers(spec.repo)  # one per call
        with bind_run(spec.run_id):
            while True:
                if found := await self._poll(github, spec, lookup):
                    log.info("question_answered", repo=spec.repo, issue=spec.number, comments=[c.id for c in found])
                    return found
                await asyncio.sleep(self._poll_s)

    @staticmethod
    async def _poll(github: GitHubApp, spec: WaitForAnswerInput, lookup: Maintainers) -> list[Comment]:
        try:
            slug = (await github.app_info()).slug
            token = await github.installation_token(spec.repo)  # cached until near expiry
            async with github.api_client(token.token) as client:
                items = await paged(client, f"/repos/{spec.repo}/issues/{spec.number}/comments")
                return await _answers(client, spec, items, slug, lookup)
        except GitHubError as exc:
            if exc.status_code in PERMANENT_STATUSES:
                raise ApplicationError(str(exc), non_retryable=True) from exc
            log.warning("issue_poll_failed", repo=spec.repo, issue=spec.number, error=str(exc))
        except httpx.HTTPError as exc:
            log.warning("issue_poll_failed", repo=spec.repo, issue=spec.number, error=f"{type(exc).__name__}: {exc}")
        return []


async def _answers(
    client: httpx.AsyncClient, spec: WaitForAnswerInput, items: list[dict], slug: str, lookup: Maintainers
) -> list[Comment]:
    """Reply once to each outsider's mention, and return the maintainers' new mentions."""
    handled = frozenset(spec.handled)
    new = [
        _comment(c)
        for c in items
        if by_person(c) and c["id"] not in handled and mentions(str(c.get("body") or ""), slug)
    ]
    new = [c for c in new if c.created_at >= spec.since]
    try:
        maintainers = await lookup.among(client, [c.author for c in new])
    except (GitHubError, httpx.HTTPError) as exc:
        status = getattr(exc, "status_code", None)
        msg = f"permission lookup failed, no author counted: {type(exc).__name__}: {exc}"
        raise ApplicationError(msg, non_retryable=status == UNAUTHORIZED) from exc
    outsiders = [Action(c.id, "comment", c.author, c.url) for c in new if c.author not in maintainers]
    posted = "\n".join(str(c.get("body") or "") for c in items)
    await reply_once(client, spec.repo, spec.number, outsiders, posted)
    return [c for c in new if c.author in maintainers]
