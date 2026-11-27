"""
Read a PR's reviews and a PR's or an issue's comments through the GitHub REST API, and pick what a review wait sees.

`review_event` decides from the PR and its reviews (oldest first, as GitHub lists them): merged or closed first; then
the first `CHANGES_REQUESTED` review the run has not handled; then an `APPROVED` review on the current head, by anyone,
not handled yet and newer than every `CHANGES_REQUESTED` review (a reviewer who accepts fact's push-back approves).
Comments are people's only: bots, fact's own replies among them, are left out.
"""

from collections.abc import Collection
from datetime import datetime
from typing import Any

import httpx
from fact.core.activity_contract import Comment, CommentKind, ReviewEvent
from fact.core.types import PRRef

PAGE_SIZE = 100
MAX_PAGES = 20  # 2,000 reviews or comments on one PR is far more than any real review
APPROVED = "APPROVED"
CHANGES_REQUESTED = "CHANGES_REQUESTED"
BOT = "Bot"


async def paged(client: httpx.AsyncClient, path: str, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Every item of a GitHub list endpoint, oldest first, page by page."""
    items: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        batch = (
            await client.get(path, params={**(params or {}), "per_page": str(PAGE_SIZE), "page": str(page)})
        ).json()
        items.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
    return items


def _login(item: dict[str, Any]) -> str:
    return str((item.get("user") or {}).get("login") or "")


def _by_person(item: dict[str, Any]) -> bool:
    user = item.get("user") or {}
    return bool(user) and user.get("type") != BOT


def review_event(
    pr: PRRef, body: dict[str, Any], reviews: list[dict[str, Any]], handled: Collection[int]
) -> ReviewEvent | None:
    """Return the first event on the PR (`body` is its `GET /pulls/{n}`), or None while nothing needs the run."""
    head = str((body.get("head") or {}).get("sha") or pr.head_sha)
    current = pr.model_copy(update={"head_sha": head})
    if body.get("merged"):
        return ReviewEvent(pr=current, kind="merged")
    if body.get("state") == "closed":
        return ReviewEvent(pr=current, kind="closed")
    last_changes = -1
    for i, review in enumerate(reviews):
        if review.get("state") != CHANGES_REQUESTED:
            continue
        last_changes = i
        if review["id"] not in handled:
            return ReviewEvent(pr=current, kind="changes_requested", review_id=review["id"], reviewer=_login(review))
    for review in reversed(reviews[last_changes + 1 :]):
        if review.get("state") == APPROVED and review.get("commit_id") == head and review["id"] not in handled:
            return ReviewEvent(pr=current, kind="approved", review_id=review["id"], reviewer=_login(review))
    return None


async def pr_reviews(client: httpx.AsyncClient, pr: PRRef) -> list[dict[str, Any]]:
    return await paged(client, f"/repos/{pr.repo}/pulls/{pr.number}/reviews")


def _time(value: object) -> datetime:
    return datetime.fromisoformat(str(value))


def _comment(item: dict[str, Any], kind: CommentKind, created: str = "created_at") -> Comment:
    review_id = item.get("id") if kind == "review" else item.get("pull_request_review_id")
    return Comment(
        id=item["id"],
        kind=kind,
        author=_login(item),
        body=str(item.get("body") or ""),
        created_at=_time(item[created]),
        url=str(item.get("html_url") or ""),
        path=str(item.get("path") or ""),
        line=item.get("line") or item.get("original_line"),
        review_id=review_id,
        in_reply_to=item.get("in_reply_to_id"),
    )


def _wanted(comment: Comment, since: datetime | None, review_id: int | None) -> bool:
    if review_id is not None and comment.review_id == review_id:
        return True
    return since is None or comment.created_at >= since


async def collect(  # noqa: PLR0913 - where to read, and from when
    client: httpx.AsyncClient,
    repo: str,
    number: int,
    *,
    pull_request: bool,
    since: datetime | None,
    review_id: int | None,
) -> list[Comment]:
    """People's comments on the PR or issue since `since` (with all of review `review_id`), oldest first."""
    found: list[Comment] = []
    if pull_request:
        reviews = await paged(client, f"/repos/{repo}/pulls/{number}/reviews")
        found += [_comment(r, "review", "submitted_at") for r in reviews if _by_person(r) and r.get("submitted_at")]
        inline = await paged(client, f"/repos/{repo}/pulls/{number}/comments")
        found += [_comment(c, "review_comment") for c in inline if _by_person(c)]
    conversation = await paged(client, f"/repos/{repo}/issues/{number}/comments")
    found += [_comment(c, "comment") for c in conversation if _by_person(c)]
    kept = [c for c in found if c.body.strip() and _wanted(c, since, review_id)]
    return sorted(kept, key=lambda c: c.created_at)
