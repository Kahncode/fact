"""
wait_for_review against a mocked GitHub API.

Merges and closes, approvals on the head only, unhandled change requests, only maintainers counting, and one reply to
each outsider's review or comment.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fact.core.activity_contract import ReviewEvent, WaitForReviewInput
from fact.core.github import GitHubApp
from fact.core.types import PRRef
from fact.worker.activities.review_wait import ReviewWaitActivities
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

REPO = "acme/widget"
TOKEN = "ghs_pr_secret_42"  # noqa: S105 - fake
HEAD = "head2"
OPEN = {"state": "open", "merged": False, "head": {"sha": HEAD}}
MERGED = {"state": "closed", "merged": True, "head": {"sha": HEAD}}
CLOSED = {"state": "closed", "merged": False, "head": {"sha": HEAD}}
MAINTAINERS = {"octo": "write", "carol": "admin"}


def _review(
    review_id: int, state: str, commit: str = HEAD, login: str = "octo", at: str = "2026-11-02T10:00:00Z"
) -> dict:
    user = {"login": login, "type": "User"}
    url = f"https://github.com/{REPO}/pull/7#pullrequestreview-{review_id}"
    return {"id": review_id, "state": state, "commit_id": commit, "user": user, "submitted_at": at, "html_url": url}


def _note(note_id: int, login: str, kind: str = "User", **extra: object) -> dict:
    url = f"https://github.com/{REPO}/pull/7#note-{note_id}"
    user = {"login": login, "type": kind}
    return {
        "id": note_id,
        "user": user,
        "body": "do it",
        "created_at": "2026-11-02T10:00:00Z",
        "html_url": url,
        **extra,
    }


def _pr(number: int) -> PRRef:
    return PRRef(repo=REPO, number=number, url=f"https://github.com/{REPO}/pull/{number}", head_sha="head1")


@dataclass
class FakeGitHub:
    """
    Answers each PR's polls with its next scripted body (the last one repeats); `None` answers 502.

    `permissions` answers the permission lookups (a login missing is not a collaborator: 404), `lookup_status` fails
    them all; every post is recorded and, unless `refuse_writes`, added to the comments it posts to.
    """

    polls: dict[int, list[dict | None]]
    reviews: dict[int, list[dict]] = field(default_factory=dict)
    comments: dict[str, list[dict]] = field(default_factory=dict)  # by path: /pulls/7/comments, /issues/7/comments
    permissions: dict[str, str] = field(default_factory=lambda: dict(MAINTAINERS))
    lookup_status: int | None = None
    refuse_writes: bool = False
    seen: list[int] = field(default_factory=list)
    lookups: list[str] = field(default_factory=list)
    writes: list[tuple[str, str]] = field(default_factory=list)

    async def handle(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911 - one answer per endpoint
        path = request.url.path
        if path == f"/repos/{REPO}/installation":
            return httpx.Response(200, json={"id": 7, "account": {"login": "acme"}})
        if path == "/app/installations/7/access_tokens":
            return httpx.Response(201, json={"token": TOKEN, "expires_at": "2099-01-01T00:00:00Z"})
        if path.endswith("/permission"):
            return self._permission(path.split("/")[-2])
        if request.method == "POST":
            return self._post(path, json.loads(request.content)["body"])
        if path.endswith("/reviews"):
            return httpx.Response(200, json=self.reviews.get(int(path.split("/")[-2]), []))
        if path.endswith("/comments"):
            return httpx.Response(200, json=self.comments.get(path.removeprefix(f"/repos/{REPO}"), []))
        number = int(path.rsplit("/", 1)[-1])
        self.seen.append(number)
        bodies = self.polls[number]
        body = bodies.pop(0) if len(bodies) > 1 else bodies[0]
        if body is None:
            return httpx.Response(502, json={"message": "Bad Gateway"})
        return httpx.Response(200, json={"number": number, **body})

    def _permission(self, login: str) -> httpx.Response:
        self.lookups.append(login)
        if self.lookup_status:
            return httpx.Response(self.lookup_status, json={"message": "API rate limit exceeded"})
        if login not in self.permissions:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json={"permission": self.permissions[login]})

    def _post(self, path: str, body: str) -> httpx.Response:
        self.writes.append((path, body))
        if self.refuse_writes:
            return httpx.Response(403, json={"message": "nope"})
        listed = "/pulls/7/comments" if path.endswith("/replies") else path.removeprefix(f"/repos/{REPO}")
        bot = {"login": "fact[bot]", "type": "Bot"}
        self.comments.setdefault(listed, []).append({"id": 900, "user": bot, "body": body})
        return httpx.Response(201, json={"id": 900})


@pytest.fixture(scope="module")
def private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


async def _wait(
    fake: FakeGitHub,
    private_key: str,
    prs: list[PRRef],
    handled: tuple[int, ...] = (),
    since: datetime | None = None,
) -> list[ReviewEvent]:
    github = GitHubApp(1, private_key, api_url="https://gh.test", transport=httpx.MockTransport(fake.handle))
    activities = ReviewWaitActivities(github, poll_interval_s=0)
    spec = WaitForReviewInput(run_id="r", prs=prs, handled_reviews=list(handled), reviews_since=since)
    return await ActivityEnvironment().run(activities.wait_for_review, spec)


def _kinds(events: list[ReviewEvent]) -> list[tuple[int, str]]:
    return [(e.pr.number, e.kind) for e in events]


async def test_open_then_merged_on_the_current_head(private_key: str) -> None:
    fake = FakeGitHub(polls={7: [OPEN, OPEN, MERGED]})
    (event,) = await _wait(fake, private_key, [_pr(7)])
    assert (event.kind, event.pr.head_sha) == ("merged", HEAD)
    assert fake.seen == [7, 7, 7]


async def test_a_round_returns_every_pr_it_found_an_event_on(private_key: str) -> None:
    fake = FakeGitHub(polls={7: [OPEN, MERGED], 8: [OPEN, OPEN, CLOSED]})
    assert _kinds(await _wait(fake, private_key, [_pr(7), _pr(8)])) == [(7, "merged")]


async def test_an_approval_on_the_head_is_returned_but_not_one_on_an_older_commit(private_key: str) -> None:
    stale = FakeGitHub(polls={7: [OPEN, MERGED]}, reviews={7: [_review(1, "APPROVED", commit="head1")]})
    assert _kinds(await _wait(stale, private_key, [_pr(7)])) == [(7, "merged")]  # the stale approval was skipped

    fresh = FakeGitHub(polls={7: [OPEN]}, reviews={7: [_review(1, "APPROVED", commit="head1"), _review(2, "APPROVED")]})
    (event,) = await _wait(fresh, private_key, [_pr(7)])
    assert (event.kind, event.review_id, event.reviewer, event.pr.head_sha) == ("approved", 2, "octo", HEAD)


async def test_a_change_request_is_returned_once_and_an_approval_must_come_after_it(private_key: str) -> None:
    reviews = [_review(1, "APPROVED"), _review(2, "CHANGES_REQUESTED", login="carol")]
    fake = FakeGitHub(polls={7: [OPEN]}, reviews={7: reviews})
    (event,) = await _wait(fake, private_key, [_pr(7)])
    assert (event.kind, event.review_id, event.reviewer) == ("changes_requested", 2, "carol")

    handled = FakeGitHub(polls={7: [OPEN, CLOSED]}, reviews={7: reviews})
    assert _kinds(await _wait(handled, private_key, [_pr(7)], (2,))) == [(7, "closed")]  # approval 1 came before it

    accepted = FakeGitHub(polls={7: [OPEN]}, reviews={7: [*reviews, _review(3, "APPROVED", login="carol")]})
    (event,) = await _wait(accepted, private_key, [_pr(7)], (2,))
    assert (event.kind, event.review_id) == ("approved", 3)


async def test_on_a_reused_pr_reviews_from_before_the_run_are_ignored(private_key: str) -> None:
    old = _review(1, "CHANGES_REQUESTED", at="2026-10-01T10:00:00Z")  # an earlier run answered it
    reviews = [old, _review(2, "APPROVED", at="2026-10-02T10:00:00Z")]
    started = datetime(2026, 11, 1, tzinfo=UTC)
    stale = FakeGitHub(polls={7: [OPEN, CLOSED]}, reviews={7: reviews})
    assert _kinds(await _wait(stale, private_key, [_pr(7)], since=started)) == [(7, "closed")]

    fresh = FakeGitHub(polls={7: [OPEN]}, reviews={7: [*reviews, _review(3, "CHANGES_REQUESTED")]})
    (event,) = await _wait(fresh, private_key, [_pr(7)], since=started)
    assert (event.kind, event.review_id) == ("changes_requested", 3)


async def test_a_transient_error_only_skips_a_poll(private_key: str) -> None:
    fake = FakeGitHub(polls={7: [None, None, CLOSED]})
    assert _kinds(await _wait(fake, private_key, [_pr(7)])) == [(7, "closed")]


async def test_only_a_maintainers_approval_counts(private_key: str) -> None:
    outsider = FakeGitHub(polls={7: [OPEN, CLOSED]}, reviews={7: [_review(1, "APPROVED", login="mallory")]})
    assert _kinds(await _wait(outsider, private_key, [_pr(7)])) == [(7, "closed")]  # not merged on mallory's word

    reader = FakeGitHub(polls={7: [OPEN, CLOSED]}, reviews={7: [_review(1, "APPROVED", login="mallory")]})
    reader.permissions["mallory"] = "read"  # anyone can read a public repository
    assert _kinds(await _wait(reader, private_key, [_pr(7)])) == [(7, "closed")]


async def test_an_outsiders_change_request_is_skipped_as_if_it_were_not_there(private_key: str) -> None:
    alone = FakeGitHub(polls={7: [OPEN, CLOSED]}, reviews={7: [_review(1, "CHANGES_REQUESTED", login="mallory")]})
    assert _kinds(await _wait(alone, private_key, [_pr(7)])) == [(7, "closed")]

    reviews = [_review(1, "APPROVED"), _review(2, "CHANGES_REQUESTED", login="mallory")]
    after_approval = FakeGitHub(polls={7: [OPEN]}, reviews={7: reviews})
    (event,) = await _wait(after_approval, private_key, [_pr(7)])
    assert (event.kind, event.review_id) == ("approved", 1)
    assert after_approval.lookups == ["octo", "mallory"]  # each login once per poll


async def test_each_outsider_action_gets_one_reply_across_polls_and_retries(private_key: str) -> None:
    reviews = [_review(1, "COMMENTED", login="mallory"), _review(2, "APPROVED"), _review(3, "APPROVED", login="eve")]
    reviews[0]["body"] = ""  # the envelope of mallory's inline comments: they are answered themselves
    comments = {
        "/pulls/7/comments": [_note(11, "mallory"), _note(12, "mallory", in_reply_to_id=11), _note(13, "octo")],
        "/issues/7/comments": [_note(21, "eve"), _note(22, "renovate[bot]", kind="Bot")],
    }
    fake = FakeGitHub(polls={7: [OPEN, OPEN, CLOSED]}, reviews={7: reviews}, comments=comments)
    assert _kinds(await _wait(fake, private_key, [_pr(7)], (2,))) == [(7, "closed")]
    fake.polls[7] = [OPEN, CLOSED]
    await _wait(fake, private_key, [_pr(7)], (2,))  # the activity retried, or the worker restarted

    pr_comments, thread = f"/repos/{REPO}/issues/7/comments", f"/repos/{REPO}/pulls/7/comments/11/replies"
    assert [(path, body.rsplit("\n", 1)[1]) for path, body in fake.writes] == [
        (pr_comments, "<!-- fact:ignored 3 -->"),
        (thread, "<!-- fact:ignored 11 -->"),
        (thread, "<!-- fact:ignored 12 -->"),
        (pr_comments, "<!-- fact:ignored 21 -->"),
    ]
    assert fake.writes[0][1].startswith(f"@eve, your [review]({reviews[2]['html_url']}): only people with write access")


async def test_a_failed_reply_does_not_stop_the_wait(private_key: str) -> None:
    fake = FakeGitHub(polls={7: [OPEN, OPEN, CLOSED]}, comments={"/issues/7/comments": [_note(21, "eve")]})
    fake.refuse_writes = True
    assert _kinds(await _wait(fake, private_key, [_pr(7)])) == [(7, "closed")]
    assert len(fake.writes) == 2  # noqa: PLR2004 - tried again on the next open poll


async def test_a_failed_permission_lookup_fails_the_activity(private_key: str) -> None:
    fake = FakeGitHub(polls={7: [OPEN]}, reviews={7: [_review(1, "APPROVED")]}, lookup_status=403)
    with pytest.raises(ApplicationError, match="permission lookup failed") as raised:
        await _wait(fake, private_key, [_pr(7)])
    assert not raised.value.non_retryable
    assert fake.writes == []
