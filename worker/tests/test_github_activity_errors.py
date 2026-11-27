"""The GitHub activities that talk to a PR share one failure contract: no App or a refused PR is never retried."""

from collections.abc import Awaitable, Callable

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fact.core.activity_contract import (
    CollectCommentsInput,
    MergeChangeSetInput,
    PostCostCommentInput,
    WaitForReviewInput,
)
from fact.core.github import GitHubApp
from fact.core.types import PRRef
from fact.worker.activities.conversation import ConversationActivities
from fact.worker.activities.cost_comment import CostCommentActivities
from fact.worker.activities.merge import MergeActivities
from fact.worker.activities.review_wait import ReviewWaitActivities
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

REPO = "acme/widget"
TOKEN = "ghs_errors_secret_42"  # noqa: S105 - fake
PR = PRRef(repo=REPO, number=7, url=f"https://github.com/{REPO}/pull/7", head_sha="abc")
RUN = {"id": "r", "steps": []}

Call = Callable[[GitHubApp | None], Awaitable[object]]


async def _post_cost_comment(github: GitHubApp | None) -> object:
    def api(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=RUN)

    http = httpx.AsyncClient(transport=httpx.MockTransport(api), base_url="http://api.test")
    activities = CostCommentActivities(github, "http://api.test", http)
    return await ActivityEnvironment().run(activities.post_cost_comment, PostCostCommentInput(run_id="r", prs=[PR]))


async def _merge(github: GitHubApp | None) -> object:
    spec = MergeChangeSetInput(run_id="r", prs=[PR])
    return await ActivityEnvironment().run(MergeActivities(github).merge_change_set, spec)


async def _wait_for_review(github: GitHubApp | None) -> object:
    activities = ReviewWaitActivities(github, poll_interval_s=0)
    return await ActivityEnvironment().run(activities.wait_for_review, WaitForReviewInput(run_id="r", prs=[PR]))


async def _collect_comments(github: GitHubApp | None) -> object:
    spec = CollectCommentsInput(run_id="r", repo=REPO, number=PR.number)
    return await ActivityEnvironment().run(ConversationActivities(github).collect_comments, spec)


ACTIVITIES = [_post_cost_comment, _merge, _wait_for_review, _collect_comments]


def _refusing_github(status: int) -> GitHubApp:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )

    async def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/repos/{REPO}/installation":
            return httpx.Response(200, json={"id": 7, "account": {"login": "acme"}})
        if path == "/app/installations/7/access_tokens":
            return httpx.Response(201, json={"token": TOKEN, "expires_at": "2099-01-01T00:00:00Z"})
        return httpx.Response(status, json={"message": "nope"})

    return GitHubApp(1, private_key.decode(), api_url="https://gh.test", transport=httpx.MockTransport(handle))


@pytest.mark.parametrize("call", ACTIVITIES)
async def test_without_a_github_app_it_is_not_retried(call: Call) -> None:
    with pytest.raises(ApplicationError) as caught:
        await call(None)
    assert caught.value.non_retryable


@pytest.mark.parametrize("call", ACTIVITIES)
async def test_a_pr_not_found_is_not_retried_and_hides_the_token(call: Call) -> None:
    with pytest.raises(ApplicationError) as caught:
        await call(_refusing_github(404))
    assert caught.value.non_retryable
    assert TOKEN not in str(caught.value)
