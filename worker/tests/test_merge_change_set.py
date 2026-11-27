"""merge_change_set against a mocked GitHub API: merged, refused or out of date (stops the rest), errors, CI guard."""

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
    states: list[str | None] = field(default_factory=lambda: ["blocked"])  # `mergeable_state` per read; None: unknown
    merged: set[int] = field(default_factory=set)  # PRs GitHub already shows as merged
    merges: list[tuple[int, dict]] = field(default_factory=list)
    state_reads: int = 0  # reads of a refused PR's `mergeable_state`
    stale_reads: int = 0  # first reads after a refusal that still describe the previous head
    heads: dict[int, str] = field(default_factory=dict)  # a PR's head on GitHub, when not the one CI passed on
    check_runs: list[dict] = field(
        default_factory=lambda: [{"name": "ci", "status": "completed", "conclusion": "success"}]
    )

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/repos/{REPO}/installation":
            return httpx.Response(200, json={"id": 7, "account": {"login": "acme"}})
        if path == "/app/installations/7/access_tokens":
            return httpx.Response(201, json={"token": TOKEN, "expires_at": "2099-01-01T00:00:00Z"})
        if path.endswith("/check-runs"):
            return httpx.Response(200, json={"total_count": len(self.check_runs), "check_runs": self.check_runs})
        if path.endswith("/status"):
            return httpx.Response(200, json={"statuses": []})
        if request.method == "GET":
            return self._pull(int(path.split("/")[-1]))
        number = int(path.split("/")[-2])
        self.merges.append((number, json.loads(request.content)))
        status = self.statuses.get(number, OK)
        if status == OK:
            self.merged.add(number)
        return httpx.Response(status, json={"merged": status == OK, "message": "done" if status == OK else "nope"})

    def _pull(self, number: int) -> httpx.Response:
        """Answer the PR: whether it is merged, before a merge; its scripted `mergeable_state` once refused."""
        if not any(n == number for n, _ in self.merges):
            head = {"sha": self.heads.get(number, f"sha{number}")}
            return httpx.Response(200, json={"merged": number in self.merged, "mergeable": None, "head": head})
        if self.stale_reads > 0:  # GitHub still describes the previous head
            self.stale_reads -= 1
            body = {"merged": False, "mergeable": True, "mergeable_state": "clean", "head": {"sha": "old"}}
            return httpx.Response(200, json=body)
        state = self.states[min(self.state_reads, len(self.states) - 1)]
        self.state_reads += 1
        mergeable = None if state is None else state != "dirty"
        body = {"merged": False, "mergeable": mergeable, "mergeable_state": state or "unknown"}
        return httpx.Response(200, json={**body, "head": {"sha": f"sha{number}"}})


@pytest.fixture(scope="module")
def private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


async def _merge(fake: FakeGitHub, private_key: str, prs: list[PRRef], method: str = "squash") -> str:
    github = GitHubApp(1, private_key, api_url="https://gh.test", transport=httpx.MockTransport(fake.handle))
    spec = MergeChangeSetInput(run_id="r", prs=prs, method=method)  # type: ignore[arg-type]
    activities = MergeActivities(github, merge_state_delay_s=0)
    return await ActivityEnvironment().run(activities.merge_change_set, spec)


async def test_merges_each_pr_at_its_head_with_the_method(private_key: str) -> None:
    fake = FakeGitHub()
    assert await _merge(fake, private_key, [_pr(7), _pr(8)], method="rebase") == "merged"
    assert fake.merges == [
        (7, {"merge_method": "rebase", "sha": "sha7"}),
        (8, {"merge_method": "rebase", "sha": "sha8"}),
    ]


@pytest.mark.parametrize("status", [405, 409, 422])
async def test_a_refused_merge_says_refused_and_stops_the_rest(private_key: str, status: int) -> None:
    fake = FakeGitHub(statuses={7: status})
    assert await _merge(fake, private_key, [_pr(7), _pr(8)]) == "refused"
    assert [n for n, _ in fake.merges] == [7]


@pytest.mark.parametrize("state", ["dirty", "behind"])
async def test_a_pr_conflicting_with_or_behind_its_base_is_out_of_date(private_key: str, state: str) -> None:
    fake = FakeGitHub(statuses={7: 405}, states=[state])
    assert await _merge(fake, private_key, [_pr(7), _pr(8)]) == "out_of_date"
    assert [n for n, _ in fake.merges] == [7]


async def test_an_unknown_merge_state_is_read_again(private_key: str) -> None:
    fake = FakeGitHub(statuses={7: 405}, states=[None, None, "dirty"])
    assert await _merge(fake, private_key, [_pr(7)]) == "out_of_date"
    assert fake.state_reads == len(fake.states)


async def test_a_merge_state_never_known_is_refused(private_key: str) -> None:
    fake = FakeGitHub(statuses={7: 405}, states=[None])
    assert await _merge(fake, private_key, [_pr(7)]) == "refused"


async def test_a_server_error_is_retried(private_key: str) -> None:
    fake = FakeGitHub(statuses={7: 502})
    with pytest.raises(ApplicationError) as caught:
        await _merge(fake, private_key, [_pr(7)])
    assert not caught.value.non_retryable


async def test_a_pr_already_merged_is_skipped(private_key: str) -> None:
    fake = FakeGitHub(merged={7})  # merged by an earlier attempt or round
    assert await _merge(fake, private_key, [_pr(7), _pr(8)]) == "merged"
    assert [n for n, _ in fake.merges] == [8]


async def test_a_stale_answer_about_the_previous_head_is_read_again(private_key: str) -> None:
    fake = FakeGitHub(statuses={7: 405}, states=["dirty"], stale_reads=1)  # "clean", but about an older head
    assert await _merge(fake, private_key, [_pr(7)]) == "out_of_date"


async def test_a_head_that_moved_after_ci_is_not_merged(private_key: str) -> None:
    fake = FakeGitHub(heads={7: "pushed-later"})
    assert await _merge(fake, private_key, [_pr(7), _pr(8)]) == "refused"
    assert fake.merges == []


@pytest.mark.parametrize(
    "check",
    [
        {"name": "ci", "status": "completed", "conclusion": "failure"},
        {"name": "ci", "status": "in_progress", "conclusion": None},
    ],
)
async def test_a_head_whose_ci_is_not_green_is_not_merged(private_key: str, check: dict) -> None:
    fake = FakeGitHub(check_runs=[check])
    assert await _merge(fake, private_key, [_pr(7)]) == "refused"
    assert fake.merges == []
