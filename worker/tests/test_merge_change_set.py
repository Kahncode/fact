"""merge_change_set against a mocked GitHub API: merged, refused (stops the rest), transient and permanent errors."""

import json
from dataclasses import dataclass, field

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fact.core.activity_contract import MergeChangeSetInput
from fact.core.github import GitHubApp
from fact.core.types import PRRef
from fact.worker.activities.merge import MergeActivities
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

REPO = "acme/widget"
TOKEN = "ghs_merge_secret_42"  # noqa: S105 - fake
OK = 200


def _pr(number: int) -> PRRef:
    return PRRef(repo=REPO, number=number, url=f"https://github.com/{REPO}/pull/{number}", head_sha=f"sha{number}")


@dataclass
class FakeGitHub:
    """Answers each PR's merge with its scripted status (200 by default) and records the request bodies."""

    statuses: dict[int, int] = field(default_factory=dict)
    merges: list[tuple[int, dict]] = field(default_factory=list)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/repos/{REPO}/installation":
            return httpx.Response(200, json={"id": 7, "account": {"login": "acme"}})
        if path == "/app/installations/7/access_tokens":
            return httpx.Response(201, json={"token": TOKEN, "expires_at": "2099-01-01T00:00:00Z"})
        number = int(path.split("/")[-2])
        self.merges.append((number, json.loads(request.content)))
        status = self.statuses.get(number, OK)
        return httpx.Response(status, json={"merged": status == OK, "message": "done" if status == OK else "nope"})


@pytest.fixture(scope="module")
def private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


async def _merge(fake: FakeGitHub, private_key: str, prs: list[PRRef], method: str = "squash") -> bool:
    github = GitHubApp(1, private_key, api_url="https://gh.test", transport=httpx.MockTransport(fake.handle))
    spec = MergeChangeSetInput(run_id="r", prs=prs, method=method)  # type: ignore[arg-type]
    return await ActivityEnvironment().run(MergeActivities(github).merge_change_set, spec)


async def test_merges_each_pr_at_its_head_with_the_method(private_key: str) -> None:
    fake = FakeGitHub()
    assert await _merge(fake, private_key, [_pr(7), _pr(8)], method="rebase")
    assert fake.merges == [
        (7, {"merge_method": "rebase", "sha": "sha7"}),
        (8, {"merge_method": "rebase", "sha": "sha8"}),
    ]


@pytest.mark.parametrize("status", [405, 409, 422])
async def test_a_refused_merge_returns_false_and_stops_the_rest(private_key: str, status: int) -> None:
    fake = FakeGitHub(statuses={7: status})
    assert not await _merge(fake, private_key, [_pr(7), _pr(8)])
    assert [n for n, _ in fake.merges] == [7]


async def test_a_server_error_is_retried(private_key: str) -> None:
    fake = FakeGitHub(statuses={7: 502})
    with pytest.raises(ApplicationError) as caught:
        await _merge(fake, private_key, [_pr(7)])
    assert not caught.value.non_retryable
