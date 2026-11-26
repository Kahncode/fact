"""post_cost_comment: render a fixture run, then create versus update the comment against a mocked GitHub API."""

import json
from dataclasses import dataclass, field

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fact.core.activity_contract import PostCostCommentInput
from fact.core.github import GitHubApp
from fact.core.types import PRRef
from fact.worker.activities.cost_comment import CostCommentActivities
from fact.worker.cost_comment import COMMENTS_PAGE, COST_MARKER, format_duration, render_cost_comment
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

FULL_NAME = "acme/widget"
PR_NUMBER = 12
PR = PRRef(repo=FULL_NAME, number=PR_NUMBER, url=f"https://github.com/{FULL_NAME}/pull/12", head_sha="abc")
TOKEN = "ghs_cost_secret_42"  # noqa: S105 - fake
COMMENTS = f"/repos/{FULL_NAME}/issues/{PR_NUMBER}/comments"


def _step(stage: str, attempt: int, start: str, end: str | None, **kw: object) -> dict:
    base = {"stage_id": stage, "attempt": attempt, "harness": None, "model": None, "wait_seconds": 0.0}
    base |= {"tokens_in": 0, "tokens_out": 0, "cost_usd": None, "started_at": start, "ended_at": end}
    return base | kw


AGENT = {"harness": "claude-code", "model": "claude-sonnet-5-5"}
RUN = {
    "id": "run-1",
    "steps": [
        _step("prepare", 1, "2026-11-25T10:00:00Z", "2026-11-25T10:00:04Z"),
        _step(
            "implement",
            1,
            "2026-11-25T10:00:05Z",
            "2026-11-25T10:12:35Z",
            tokens_in=120_000,
            tokens_out=8_000,
            cache_read_tokens=100_000,
            cache_write_tokens=15_000,
            cost_usd="1.2345",
            **AGENT,
        ),
        _step("checks", 1, "2026-11-25T10:12:40Z", "2026-11-25T10:13:40Z", wait_seconds=30.0),
        _step(
            "implement",
            2,
            "2026-11-25T10:14:00Z",
            "2026-11-25T10:16:00Z",
            tokens_in=20_000,
            tokens_out=1_000,
            cost_usd="0.25",
            **AGENT,
        ),
        _step("open_pr", 1, "2026-11-25T10:16:10Z", None),
    ],
}
CUT_SHORT = _step(
    "fix_ci",
    1,
    "2026-11-25T10:20:00Z",
    "2026-11-25T10:21:00Z",
    tokens_in=3_000,
    tokens_out=200,
    cache_read_tokens=2_000,
    usage_partial=True,
    **AGENT,
)


def test_render_from_a_fixture_run() -> None:
    body = render_cost_comment(RUN)
    lines = body.splitlines()

    assert lines[0] == COST_MARKER
    assert "**fact run cost:** $1.48 over 15m 34s (5 steps)" in body
    route = "claude-code / claude-sonnet-5-5"
    assert "| prepare | 1 | — | 4s | — | — | — | — | — | — |" in lines
    assert f"| implement | 1 | {route} | 12m 30s | — | 5,000 | 100,000 | 15,000 | 8,000 | $1.23 |" in lines
    assert "| checks | 1 | — | 1m 00s | 30s | — | — | — | — | — |" in lines
    assert "| open_pr | 1 | — | running | — | — | — | — | — | — |" in lines
    assert "| **Total** | 5 steps |  | 15m 34s | 30s | 25,000 | 100,000 | 15,000 | 9,000 | $1.48 |" in lines
    assert f"| {route} | 2 | 14m 30s | 25,000 | 100,000 | 15,000 | 9,000 | $1.48 |" in lines
    assert "unknown" not in body.split("<sub>")[0]
    assert "run-1" in body


def test_a_cut_short_session_shows_its_tokens_and_an_unknown_cost() -> None:
    body = render_cost_comment({"id": "run-1", "steps": [*RUN["steps"], CUT_SHORT]})
    lines = body.splitlines()
    route = "claude-code / claude-sonnet-5-5"
    assert f"| fix_ci (cut short) | 1 | {route} | 1m 00s | — | 1,000 | 2,000 | — | 200 | unknown |" in lines
    assert "**fact run cost:** $1.48 over 16m 34s (6 steps); cost not reported for 1 agent steps" in body
    assert "| **Total** | 6 steps |  | 16m 34s | 30s | 26,000 | 102,000 | 15,000 | 9,200 | $1.48 + 1 unknown |" in lines


def test_render_a_run_without_agents() -> None:
    body = render_cost_comment({"id": "run-2", "steps": [_step("prepare", 1, "2026-11-25T10:00:00Z", None)]})
    assert "$0.00" in body
    assert "Per harness and model" not in body


@pytest.mark.parametrize(
    ("seconds", "text"), [(None, "running"), (0, "0s"), (59.6, "1m 00s"), (3599, "59m 59s"), (3720, "1h 02m")]
)
def test_format_duration(seconds: float | None, text: str) -> None:
    assert format_duration(seconds) == text


@dataclass
class FakeGitHub:
    comments: list[dict] = field(default_factory=list)
    requests: list[tuple[str, str]] = field(default_factory=list)
    fail_with: int | None = None

    async def handle(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        if path == f"/repos/{FULL_NAME}/installation":
            return httpx.Response(200, json={"id": 7, "account": {"login": "acme"}})
        if path == "/app/installations/7/access_tokens":
            return httpx.Response(201, json={"token": TOKEN, "expires_at": "2099-01-01T00:00:00Z"})
        self.requests.append((method, path))
        if self.fail_with:
            return httpx.Response(self.fail_with, json={"message": "nope"})
        if path == COMMENTS and method == "GET":
            page = int(request.url.params["page"])
            return httpx.Response(200, json=self.comments[(page - 1) * COMMENTS_PAGE : page * COMMENTS_PAGE])
        if path == COMMENTS and method == "POST":
            self.comments.append({"id": 1000 + len(self.comments), "body": body["body"]})
            return httpx.Response(201, json=self.comments[-1])
        prefix = f"/repos/{FULL_NAME}/issues/comments/"
        if path.startswith(prefix) and method == "PATCH":
            comment = next(c for c in self.comments if c["id"] == int(path.removeprefix(prefix)))
            comment["body"] = body["body"]
            return httpx.Response(200, json=comment)
        return httpx.Response(404, json={"message": "Not Found"})


@pytest.fixture(scope="module")
def private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def _api(run: dict | None) -> httpx.AsyncClient:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/runs/run-1"
        return httpx.Response(200, json=run) if run else httpx.Response(404, json={"detail": "run not found"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://api.test")


def _activities(fake: FakeGitHub, private_key: str, run: dict | None = RUN) -> CostCommentActivities:
    github = GitHubApp(1, private_key, api_url="https://gh.test", transport=httpx.MockTransport(fake.handle))
    return CostCommentActivities(github, "http://api.test", _api(run))


async def _post(activities: CostCommentActivities) -> None:
    await ActivityEnvironment().run(activities.post_cost_comment, PostCostCommentInput(run_id="run-1", prs=[PR]))


async def test_creates_then_updates_the_comment(private_key: str) -> None:
    others = [{"id": i, "body": f"review {i}"} for i in range(COMMENTS_PAGE)]  # a full first page of other comments
    fake = FakeGitHub(comments=list(others))
    activities = _activities(fake, private_key)

    await _post(activities)
    assert ("POST", COMMENTS) in fake.requests
    assert [c["id"] for c in fake.comments if COST_MARKER in c["body"]] == [1100]

    fake.requests.clear()
    await _post(activities)
    assert ("POST", COMMENTS) not in fake.requests
    assert ("PATCH", f"/repos/{FULL_NAME}/issues/comments/1100") in fake.requests
    assert len(fake.comments) == COMMENTS_PAGE + 1  # still one cost comment
    assert fake.comments[-1]["body"] == render_cost_comment(RUN)


async def test_updates_a_comment_left_by_an_earlier_run(private_key: str) -> None:
    fake = FakeGitHub(comments=[{"id": 5, "body": "lgtm"}, {"id": 6, "body": f"{COST_MARKER}\nold numbers"}])
    await _post(_activities(fake, private_key))
    assert ("PATCH", f"/repos/{FULL_NAME}/issues/comments/6") in fake.requests
    assert ("POST", COMMENTS) not in fake.requests
    assert fake.comments[1]["body"] == render_cost_comment(RUN)


async def test_unknown_run_is_not_retried(private_key: str) -> None:
    fake = FakeGitHub()
    with pytest.raises(ApplicationError, match="run-1") as caught:
        await _post(_activities(fake, private_key, run=None))
    assert caught.value.non_retryable
    assert fake.requests == []


async def test_pr_not_found_is_not_retried(private_key: str) -> None:
    fake = FakeGitHub(fail_with=404)
    with pytest.raises(ApplicationError) as caught:
        await _post(_activities(fake, private_key))
    assert caught.value.non_retryable


async def test_no_github_app_is_not_retried() -> None:
    activities = CostCommentActivities(None, "http://api.test", _api(RUN))
    with pytest.raises(ApplicationError, match="GitHub App not configured") as caught:
        await _post(activities)
    assert caught.value.non_retryable
