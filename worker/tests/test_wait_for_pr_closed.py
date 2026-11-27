"""wait_for_pr_closed against a mocked GitHub API: open then merged or closed, transient and permanent errors."""

from dataclasses import dataclass, field

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fact.core.activity_contract import WaitForPRClosedInput
from fact.core.github import GitHubApp
from fact.core.types import PRRef
from fact.worker.activities.pr_state import PRStateActivities
from temporalio.testing import ActivityEnvironment

REPO = "acme/widget"
TOKEN = "ghs_pr_secret_42"  # noqa: S105 - fake
OPEN = {"state": "open", "merged": False}
MERGED = {"state": "closed", "merged": True}
CLOSED = {"state": "closed", "merged": False}


def _pr(number: int) -> PRRef:
    return PRRef(repo=REPO, number=number, url=f"https://github.com/{REPO}/pull/{number}", head_sha="abc")


@dataclass
class FakeGitHub:
    """Answers each PR's polls with its next scripted body (the last one repeats); `None` answers 502."""

    polls: dict[int, list[dict | None]]
    fail_with: int | None = None
    seen: list[int] = field(default_factory=list)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/repos/{REPO}/installation":
            return httpx.Response(200, json={"id": 7, "account": {"login": "acme"}})
        if path == "/app/installations/7/access_tokens":
            return httpx.Response(201, json={"token": TOKEN, "expires_at": "2099-01-01T00:00:00Z"})
        number = int(path.rsplit("/", 1)[-1])
        self.seen.append(number)
        if self.fail_with:
            return httpx.Response(self.fail_with, json={"message": "nope"})
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


async def _wait(fake: FakeGitHub, private_key: str, prs: list[PRRef]) -> list[str]:
    github = GitHubApp(1, private_key, api_url="https://gh.test", transport=httpx.MockTransport(fake.handle))
    activities = PRStateActivities(github, poll_interval_s=0)
    spec = WaitForPRClosedInput(run_id="r", prs=prs)
    return await ActivityEnvironment().run(activities.wait_for_pr_closed, spec)


async def test_open_then_merged(private_key: str) -> None:
    fake = FakeGitHub(polls={7: [OPEN, OPEN, MERGED]})
    assert await _wait(fake, private_key, [_pr(7)]) == ["merged"]
    assert fake.seen == [7, 7, 7]


async def test_each_pr_is_polled_until_it_ends(private_key: str) -> None:
    fake = FakeGitHub(polls={7: [MERGED], 8: [OPEN, OPEN, CLOSED]})
    assert await _wait(fake, private_key, [_pr(7), _pr(8)]) == ["merged", "closed"]
    assert fake.seen == [7, 8, 8, 8]  # a merged PR is not polled again


async def test_a_transient_error_only_skips_a_poll(private_key: str) -> None:
    fake = FakeGitHub(polls={7: [None, None, CLOSED]})
    assert await _wait(fake, private_key, [_pr(7)]) == ["closed"]
