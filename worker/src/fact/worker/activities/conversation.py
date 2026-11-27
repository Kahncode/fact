"""
The GitHub side of a conversation (`fact.sdk.converse`) and of a review round, all through the GitHub App (`fact[bot]`).

- `collect_comments`: maintainers' comments on a PR or an issue since a point, with their ids: review bodies, inline
  review comments and conversation comments (an issue has only the last). Anyone else's are left out
  (`fact.worker.reviews.Maintainers`); a permission lookup that fails other than with a 404 fails the attempt.
- `post_replies`: post a session's replies. A reply to an inline review comment goes in its thread; any other is a
  comment on the PR or issue that quotes what it answers. Each carries a hidden key, and a reply whose key is already
  there is skipped, so a retried attempt never posts twice. A reply to a comment the session never read is dropped.
  Threads are never resolved: that is the reviewer's call.
- `request_review`: ask the reviewers for a review again; GitHub refusing (such as a reviewer without access) is logged.

A transient GitHub error is retried by Temporal; bad credentials or a missing PR or App fail without a retry.
"""

from collections.abc import Awaitable, Callable

import httpx
from fact.core.activity_contract import (
    COLLECT_COMMENTS,
    POST_REPLIES,
    REQUEST_REVIEW,
    CollectCommentsInput,
    Comment,
    PostRepliesInput,
    Reply,
    RequestReviewInput,
)
from fact.core.github import GitHubApp, GitHubError
from fact.core.logging import bind_run, get_logger
from fact.worker.reviews import collect, paged
from temporalio import activity
from temporalio.exceptions import ApplicationError

log = get_logger(__name__)

PERMANENT_STATUSES = frozenset({401, 404})  # bad credentials, PR or App not found
REFUSED = 422
NO_APP = "GitHub App not configured: set FACT_GITHUB_APP_ID and FACT_GITHUB_APP_PRIVATE_KEY_PATH"
QUOTE_CHARS = 300


def reply_marker(key: str, comment_id: int) -> str:
    return f"<!-- fact:reply {key}:{comment_id} -->"


def _quote(comment: Comment) -> str:
    text = comment.body.strip()
    text = text if len(text) <= QUOTE_CHARS else text[:QUOTE_CHARS] + "…"
    quoted = "\n".join(f"> {line}" for line in text.splitlines())
    where = f"[@{comment.author}]({comment.url})" if comment.url else f"@{comment.author}"
    return f"{where} wrote:\n{quoted}"


class ConversationActivities:
    """Control zone: reads and writes comments with the GitHub App."""

    def __init__(self, github: GitHubApp | None) -> None:
        self._github = github

    async def _with_client[T](self, repo: str, work: Callable[[httpx.AsyncClient], Awaitable[T]]) -> T:
        if self._github is None:
            raise ApplicationError(NO_APP, non_retryable=True)
        try:
            token = await self._github.installation_token(repo)
            async with self._github.api_client(token.token) as client:
                return await work(client)
        except GitHubError as exc:
            raise ApplicationError(str(exc), non_retryable=exc.status_code in PERMANENT_STATUSES) from exc
        except httpx.HTTPError as exc:
            msg = f"GitHub request failed: {type(exc).__name__}: {exc}"
            raise ApplicationError(msg) from exc

    @activity.defn(name=COLLECT_COMMENTS)
    async def collect_comments(self, spec: CollectCommentsInput) -> list[Comment]:
        with bind_run(spec.run_id):

            async def work(client: httpx.AsyncClient) -> list[Comment]:
                return await collect(
                    client,
                    spec.repo,
                    spec.number,
                    pull_request=spec.pull_request,
                    since=spec.since,
                    review_id=spec.review_id,
                )

            comments = await self._with_client(spec.repo, work)
            log.info("comments_collected", repo=spec.repo, number=spec.number, comments=len(comments))
            return comments

    @activity.defn(name=POST_REPLIES)
    async def post_replies(self, spec: PostRepliesInput) -> int:
        """Post each reply once; return how many were posted by this attempt."""
        with bind_run(spec.run_id):
            return await self._with_client(spec.repo, lambda client: _post_all(client, spec))

    @activity.defn(name=REQUEST_REVIEW)
    async def request_review(self, spec: RequestReviewInput) -> None:
        pr = spec.pr
        reviewers = [r for r in spec.reviewers if r]
        if not reviewers:
            return
        with bind_run(spec.run_id):

            async def work(client: httpx.AsyncClient) -> None:
                path = f"/repos/{pr.repo}/pulls/{pr.number}/requested_reviewers"
                try:
                    await client.post(path, json={"reviewers": reviewers})
                except GitHubError as exc:
                    if exc.status_code != REFUSED:
                        raise
                    log.warning("review_request_refused", pr=pr.url, reviewers=reviewers, error=str(exc))
                    return
                log.info("review_requested", pr=pr.url, reviewers=reviewers)

            await self._with_client(pr.repo, work)


async def _posted_markers(client: httpx.AsyncClient, spec: PostRepliesInput) -> str:
    """Every body fact may have posted already on the PR or issue, joined, to look for reply markers in."""
    bodies = [c.get("body") or "" for c in await paged(client, f"/repos/{spec.repo}/issues/{spec.number}/comments")]
    if any(c.kind == "review_comment" for c in spec.comments):
        inline = await paged(client, f"/repos/{spec.repo}/pulls/{spec.number}/comments")
        bodies += [c.get("body") or "" for c in inline]
    return "\n".join(bodies)


async def _post_all(client: httpx.AsyncClient, spec: PostRepliesInput) -> int:
    by_id = {c.id: c for c in spec.comments}
    posted_before = await _posted_markers(client, spec)
    posted = 0
    for reply in spec.replies:
        comment = by_id.get(reply.comment_id)
        marker = reply_marker(spec.key, reply.comment_id)
        if comment is None or not reply.body.strip():
            log.warning("reply_dropped", comment=reply.comment_id, reason="not a comment the session read, or empty")
            continue
        if marker in posted_before:
            continue
        await _post(client, spec, comment, reply, marker)
        posted += 1
    log.info("replies_posted", repo=spec.repo, number=spec.number, posted=posted, replies=len(spec.replies))
    return posted


async def _post(client: httpx.AsyncClient, spec: PostRepliesInput, comment: Comment, reply: Reply, marker: str) -> None:
    if comment.kind == "review_comment":
        thread = comment.in_reply_to or comment.id  # GitHub takes replies on a thread's first comment only
        path = f"/repos/{spec.repo}/pulls/{spec.number}/comments/{thread}/replies"
        await client.post(path, json={"body": f"{reply.body}\n\n{marker}"})
        return
    body = f"{_quote(comment)}\n\n{reply.body}\n\n{marker}"
    await client.post(f"/repos/{spec.repo}/issues/{spec.number}/comments", json={"body": body})
