"""wait_for_review against a mocked GitHub API: merges and closes, approvals on the head only, unhandled change requests."""

from dataclasses import dataclass, field

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fact.core.activity_contract import ReviewEvent, WaitForReviewInput
from fact.core.github import GitHubApp
from fact.core.types import PRRef
from fact.worker.activities.review_wait import ReviewWaitActivities
from temporalio.testing import ActivityEnvironment

REPO = "acme/widget"
TOKEN = "ghs_pr_secret_42"  # noqa: S105 - fake
HEAD = "head2"
OPEN = {"state": "open", "merged": False, "head": {"sha": HEAD}}
MERGED = {"state": "closed", "merged": True, "head": {"sha": HEAD}}
CLOSED = {"state": "closed", "merged": False, "head": {"sha": HEAD}}


def _review(review_id: int, state: str, commit: str = HEAD, login: str = "octo") -> dict:
    return {"id": review_id, "state": state, "commit_id": commit, "user": {"login": login, "type": "User"}}


def _pr(number: int) -> PRRef:
    return PRRef(repo=REPO, number=number, url=f"https://github.com/{REPO}/pull/{number}", head_sha="head1")


@dataclass
class FakeGitHub:
    """Answers each PR's polls with its next scripted body (the last one repeats); `None` answers 502."""

    polls: dict[int, list[dict | None]]
    reviews: dict[int, list[dict]] = field(default_factory=dict)
    seen: list[int] = field(default_factory=list)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/repos/{REPO}/installation":
            return httpx.Response(200, json={"id": 7, "account": {"login": "acme"}})
        if path == "/app/installations/7/access_tokens":
            return httpx.Response(201, json={"token": TOKEN, "expires_at": "2099-01-01T00:00:00Z"})
        if path.endswith("/reviews"):
            return httpx.Response(200, json=self.reviews.get(int(path.split("/")[-2]), []))
        number = int(path.rsplit("/", 1)[-1])
        self.seen.append(number)
        bodies = self.polls[number]
        body = bodies.pop(0) if len(bodies) > 1 else bodies[0]
        if body is None:
            return httpx.Response(502, json={"message": "Bad Gateway"})
        return httpx.Response(200, json={"number": number, **body})


@pytest.fixture(scope="module")
def private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


async def _wait(
    fake: FakeGitHub, private_key: str, prs: list[PRRef], handled: tuple[int, ...] = ()
) -> list[ReviewEvent]:
    github = GitHubApp(1, private_key, api_url="https://gh.test", transport=httpx.MockTransport(fake.handle))
    activities = ReviewWaitActivities(github, poll_interval_s=0)
    spec = WaitForReviewInput(run_id="r", prs=prs, handled_reviews=list(handled))
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


async def test_a_transient_error_only_skips_a_poll(private_key: str) -> None:
    fake = FakeGitHub(polls={7: [None, None, CLOSED]})
    assert _kinds(await _wait(fake, private_key, [_pr(7)])) == [(7, "closed")]
