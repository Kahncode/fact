"""collect_comments, post_replies and request_review against a mocked GitHub API."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fact.core.activity_contract import (
    CollectCommentsInput,
    Comment,
    PostRepliesInput,
    Reply,
    RequestReviewInput,
)
from fact.core.github import GitHubApp
from fact.core.types import PRRef
from fact.worker.activities.conversation import ConversationActivities, reply_marker
from temporalio.testing import ActivityEnvironment

REPO = "acme/widget"
TOKEN = "ghs_conv_secret_42"  # noqa: S105 - fake
PERSON = {"login": "octo", "type": "User"}
BOT = {"login": "sofact-dev[bot]", "type": "Bot"}
PUSHED = datetime(2026, 11, 27, 12, tzinfo=UTC)
BEFORE, AFTER = "2026-11-27T11:00:00Z", "2026-11-27T13:00:00Z"


@dataclass
class FakeGitHub:
    """Serves scripted lists per path; records every write."""

    lists: dict[str, list[dict]] = field(default_factory=dict)
    writes: list[tuple[str, dict]] = field(default_factory=list)
    refuse_writes: int | None = None

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/repos/{REPO}/installation":
            return httpx.Response(200, json={"id": 7, "account": {"login": "acme"}})
        if path == "/app/installations/7/access_tokens":
            return httpx.Response(201, json={"token": TOKEN, "expires_at": "2099-01-01T00:00:00Z"})
        if request.method == "GET":
            return httpx.Response(200, json=self.lists.get(path, []))
        if self.refuse_writes:
            return httpx.Response(self.refuse_writes, json={"message": "nope"})
        self.writes.append((path, json.loads(request.content)))
        return httpx.Response(201, json={"id": 1})


@pytest.fixture(scope="module")
def private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def _activities(fake: FakeGitHub, private_key: str) -> ConversationActivities:
    transport = httpx.MockTransport(fake.handle)
    return ConversationActivities(GitHubApp(1, private_key, api_url="https://gh.test", transport=transport))


async def test_collect_reads_peoples_comments_since_the_push_and_all_of_the_review(private_key: str) -> None:
    pulls = f"/repos/{REPO}/pulls/7"
    fake = FakeGitHub(
        lists={
            f"{pulls}/reviews": [
                {"id": 9, "user": PERSON, "body": "Please change", "state": "CHANGES_REQUESTED", "submitted_at": AFTER},
                {"id": 8, "user": PERSON, "body": "", "state": "APPROVED", "submitted_at": AFTER},  # nothing to read
            ],
            f"{pulls}/comments": [
                {"id": 21, "user": PERSON, "body": "old, but in the review", "created_at": BEFORE,
                 "pull_request_review_id": 9, "path": "w.py", "line": 3},
                {"id": 22, "user": PERSON, "body": "old", "created_at": BEFORE, "pull_request_review_id": 5},
                {"id": 23, "user": BOT, "body": "fact's reply", "created_at": AFTER, "in_reply_to_id": 21},
            ],
            f"/repos/{REPO}/issues/7/comments": [{"id": 31, "user": PERSON, "body": "also", "created_at": AFTER}],
        }
    )  # fmt: skip
    spec = CollectCommentsInput(run_id="r", repo=REPO, number=7, since=PUSHED, review_id=9)
    comments = await ActivityEnvironment().run(_activities(fake, private_key).collect_comments, spec)

    assert [(c.id, c.kind) for c in comments] == [(21, "review_comment"), (9, "review"), (31, "comment")]
    assert (comments[0].path, comments[0].line, comments[0].review_id) == ("w.py", 3, 9)


def _comment(comment_id: int, kind: str, in_reply_to: int | None = None) -> Comment:
    return Comment(
        id=comment_id, kind=kind, author="octo", body="Rename it", created_at=PUSHED, in_reply_to=in_reply_to
    )  # type: ignore[arg-type]


async def test_replies_go_in_their_thread_or_on_the_pr_once_each(private_key: str) -> None:
    posted = f"already\n\n{reply_marker('k', 31)}"
    fake = FakeGitHub(lists={f"/repos/{REPO}/issues/7/comments": [{"id": 40, "user": BOT, "body": posted}]})
    comments = [_comment(22, "review_comment", in_reply_to=21), _comment(9, "review"), _comment(31, "comment")]
    replies = [Reply(comment_id=i, body=f"re {i}") for i in (22, 9, 31, 99)]  # 31 posted before, 99 never read
    spec = PostRepliesInput(run_id="r", repo=REPO, number=7, comments=comments, replies=replies, key="k")
    count = await ActivityEnvironment().run(_activities(fake, private_key).post_replies, spec)

    assert count == 2  # noqa: PLR2004
    (thread, thread_body), (pr, pr_body) = fake.writes
    assert (thread, thread_body["body"]) == (
        f"/repos/{REPO}/pulls/7/comments/21/replies",
        f"re 22\n\n{reply_marker('k', 22)}",
    )
    assert pr == f"/repos/{REPO}/issues/7/comments"
    assert pr_body["body"].startswith("@octo wrote:\n> Rename it\n\nre 9")


async def test_a_refused_review_request_is_not_an_error(private_key: str) -> None:
    fake = FakeGitHub(refuse_writes=422)
    pr = PRRef(repo=REPO, number=7, url=f"https://github.com/{REPO}/pull/7", head_sha="abc")
    spec = RequestReviewInput(run_id="r", pr=pr, reviewers=["octo"])
    await ActivityEnvironment().run(_activities(fake, private_key).request_review, spec)
    assert fake.writes == []
