"""Find, create and update a branch's pull request through the GitHub REST API."""

from dataclasses import dataclass
from typing import Any

import httpx

BODY_MAX_CHARS = 65_000  # GitHub rejects PR bodies over 65,536 characters


@dataclass(frozen=True)
class RepoInfo:
    full_name: str
    default_branch: str
    html_url: str  # web URL of the repository, such as https://github.com/owner/repo


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str  # web URL
    created: bool  # False when an open PR for the branch was reused


async def repo_info(client: httpx.AsyncClient, full_name: str) -> RepoInfo:
    body = (await client.get(f"/repos/{full_name}")).json()
    return RepoInfo(full_name=full_name, default_branch=body["default_branch"], html_url=body["html_url"])


async def find_open_pr(client: httpx.AsyncClient, full_name: str, branch: str, base: str) -> dict[str, Any] | None:
    """Return the open PR from `branch` in this repository (not a fork) into `base`, or None."""
    owner = full_name.partition("/")[0]
    params = {"state": "open", "head": f"{owner}:{branch}", "base": base, "per_page": "1"}
    pulls = (await client.get(f"/repos/{full_name}/pulls", params=params)).json()
    return pulls[0] if pulls else None


def _limit(body: str) -> str:
    return body if len(body) <= BODY_MAX_CHARS else body[: BODY_MAX_CHARS - 20] + "\n\n…(truncated)\n"


async def open_or_update_pr(
    client: httpx.AsyncClient,
    repo: RepoInfo,
    *,
    branch: str,
    title: str,
    body: str,
) -> PullRequest:
    """Reuse the branch's open PR and replace its body, or open a new one against the default branch."""
    body = _limit(body)
    existing = await find_open_pr(client, repo.full_name, branch, repo.default_branch)
    if existing is not None:
        number = existing["number"]
        pr = (await client.patch(f"/repos/{repo.full_name}/pulls/{number}", json={"body": body})).json()
        return PullRequest(number=pr["number"], url=pr["html_url"], created=False)
    payload = {"title": title, "head": branch, "base": repo.default_branch, "body": body}
    pr = (await client.post(f"/repos/{repo.full_name}/pulls", json=payload)).json()
    return PullRequest(number=pr["number"], url=pr["html_url"], created=True)
