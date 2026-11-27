"""
Tell an outsider, once, that fact did not take their review or comment into account.

Only maintainers steer fact (`fact.worker.reviews.Maintainers`). Each review or comment a person without write access
left on a PR a run waits on gets one fixed reply from `fact[bot]`, written by no agent: a review or a conversation
comment gets a PR comment linking it, an inline review comment a reply in its thread. Each reply carries
`<!-- fact:ignored <id> -->`, and one whose marker is already on the PR is not posted again, so polls, retries and
restarts post it once. A failed reply is logged and skipped: it never stops the wait.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import httpx
from fact.core.github import GitHubError
from fact.core.logging import get_logger
from fact.core.types import PRRef
from fact.worker.reviews import by_person, login_of

log = get_logger(__name__)

STEER = "only people with write access to this repository can steer fact, so it was not taken into account."
COMMENTED = "COMMENTED"

type Kind = Literal["review", "review_comment", "comment"]


def ignored_marker(item_id: int) -> str:
    return f"<!-- fact:ignored {item_id} -->"


@dataclass(frozen=True)
class Action:
    """A person's review or comment on a PR."""

    id: int
    kind: Kind
    login: str
    url: str
    thread: int | None = None  # an inline comment's thread (its first comment)


def candidates(
    reviews: list[dict[str, Any]],
    inline: list[dict[str, Any]],
    conversation: list[dict[str, Any]],
    since: datetime | None,
) -> list[Action]:
    """
    Every review and comment by a person since `since`, whoever wrote it.

    A `COMMENTED` review with no body is only the envelope of its inline comments, which are answered themselves.
    """
    found: list[Action] = []
    for r in reviews:
        if not r.get("submitted_at") or (r.get("state") == COMMENTED and not str(r.get("body") or "").strip()):
            continue
        if by_person(r) and _since(r["submitted_at"], since):
            found.append(Action(r["id"], "review", login_of(r), str(r.get("html_url") or "")))
    for c in inline:
        if by_person(c) and _since(c.get("created_at"), since):
            thread = c.get("in_reply_to_id") or c["id"]
            found.append(Action(c["id"], "review_comment", login_of(c), str(c.get("html_url") or ""), thread))
    found.extend(
        Action(c["id"], "comment", login_of(c), str(c.get("html_url") or ""))
        for c in conversation
        if by_person(c) and _since(c.get("created_at"), since)
    )
    return found


async def reply_once(client: httpx.AsyncClient, pr: PRRef, ignored: list[Action], posted: str) -> int:
    """Reply to each of `ignored` whose marker is not in `posted` (the PR's comment bodies); return how many."""
    count = 0
    for item in ignored:
        marker = ignored_marker(item.id)
        if marker in posted:
            continue
        event = "review_ignored" if item.kind == "review" else "comment_ignored"
        log.info(event, pr=pr.url, login=item.login, id=item.id, kind=item.kind, reason="not_maintainer")
        try:
            await _post(client, pr, item, marker)
        except (GitHubError, httpx.HTTPError) as exc:
            log.warning("ignored_reply_failed", pr=pr.url, id=item.id, error=f"{type(exc).__name__}: {exc}")
            continue
        count += 1
    return count


async def _post(client: httpx.AsyncClient, pr: PRRef, item: Action, marker: str) -> None:
    if item.kind == "review_comment":
        path = f"/repos/{pr.repo}/pulls/{pr.number}/comments/{item.thread}/replies"
        await client.post(path, json={"body": f"@{item.login}, this comment: {STEER}\n\n{marker}"})
        return
    noun = "review" if item.kind == "review" else "comment"
    what = f"[{noun}]({item.url})" if item.url else noun
    body = f"@{item.login}, your {what}: {STEER}\n\n{marker}"
    await client.post(f"/repos/{pr.repo}/issues/{pr.number}/comments", json={"body": body})


def _since(value: object, since: datetime | None) -> bool:
    return since is None or not value or datetime.fromisoformat(str(value)) >= since
