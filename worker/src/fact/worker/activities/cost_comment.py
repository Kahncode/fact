"""`post_cost_comment`: put the run's time and cost, per step and per harness and model, on each of its PRs."""

from typing import Any

import httpx
from fact.core.activity_contract import POST_COST_COMMENT, PostCostCommentInput
from fact.core.github import GitHubApp, GitHubError
from fact.core.logging import bind_run, get_logger
from fact.worker.cost_comment import render_cost_comment, upsert_comment
from temporalio import activity
from temporalio.exceptions import ApplicationError

log = get_logger(__name__)

API_PREFIX = "/api/v1"
SERVER_ERROR = 500
PERMANENT_STATUSES = frozenset({401, 404, 422})  # bad credentials, PR or App not found, comment rejected


def _non_retryable(message: str) -> ApplicationError:
    return ApplicationError(message, non_retryable=True)


class CostCommentActivities:
    """Control zone: reads the run from the runs API and writes the comment with the GitHub App."""

    def __init__(self, github: GitHubApp | None, api_base_url: str, http_client: httpx.AsyncClient) -> None:
        self._github = github
        self._base = api_base_url.rstrip("/") + API_PREFIX
        self._http = http_client

    @activity.defn(name=POST_COST_COMMENT)
    async def post_cost_comment(self, spec: PostCostCommentInput) -> None:
        with bind_run(spec.run_id):
            github = self._github
            if github is None:
                msg = "GitHub App not configured: set FACT_GITHUB_APP_ID and FACT_GITHUB_APP_PRIVATE_KEY_PATH"
                raise _non_retryable(msg)
            body = render_cost_comment(await self._run(spec.run_id))
            for pr in spec.prs:
                try:
                    created = await _comment(github, pr.repo, pr.number, body)
                except GitHubError as exc:
                    if exc.status_code in PERMANENT_STATUSES:
                        raise _non_retryable(str(exc)) from exc
                    raise
                log.info("cost_comment_posted", pr=pr.url, created=created)

    async def _run(self, run_id: str) -> dict[str, Any]:
        """Fetch the run with its steps; a 4xx will not get better on retry."""
        try:
            response = await self._http.get(f"{self._base}/runs/{run_id}")
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < SERVER_ERROR:
                msg = f"runs API refused run {run_id}: {exc.response.status_code} {exc.response.text}"
                raise _non_retryable(msg) from exc
            raise
        return response.json()


async def _comment(github: GitHubApp, full_name: str, number: int, body: str) -> bool:
    token = await github.installation_token(full_name)
    async with github.api_client(token.token) as client:
        return await upsert_comment(client, full_name, number, body)
