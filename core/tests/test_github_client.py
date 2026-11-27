import httpx
import pytest
from fact.core.github import (
    GitHubAuthError,
    GitHubError,
    GitHubForbiddenError,
    GitHubNotFoundError,
    github_client,
)

SECRET = "ghs_supersecret"  # noqa: S105 fake  # pragma: allowlist secret


def _client(status: int, body: object = None, *, token: str | None = SECRET) -> httpx.AsyncClient:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return github_client(token, api_url="https://gh.test/", transport=httpx.MockTransport(handler))


async def test_headers_and_base_url() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    async with github_client(SECRET, api_url="https://gh.test/", transport=httpx.MockTransport(handler)) as c:
        await c.get("/app")

    (request,) = seen
    assert str(request.url) == "https://gh.test/app"
    assert request.headers["Authorization"] == f"Bearer {SECRET}"
    assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert request.headers["Accept"] == "application/vnd.github+json"


@pytest.mark.parametrize(
    ("status", "cls", "hint"),
    [
        (401, GitHubAuthError, "rejected the credentials"),
        (403, GitHubForbiddenError, "lacks a permission"),
        (404, GitHubNotFoundError, "installed"),
        (500, GitHubError, "request failed"),
    ],
)
async def test_errors(status: int, cls: type[GitHubError], hint: str) -> None:
    async with _client(status, {"message": "Bad credentials"}) as c:
        with pytest.raises(cls) as exc:
            await c.get("/repos/o/r/installation")

    message = str(exc.value)
    assert type(exc.value) is cls
    assert exc.value.status_code == status
    assert hint in message
    assert f"({status} on GET /repos/o/r/installation)" in message
    assert message.endswith(": Bad credentials")
    assert SECRET not in message


async def test_error_with_non_json_body() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    async with github_client(SECRET, api_url="https://gh.test", transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(GitHubError, match="bad gateway"):
            await c.get("/app")
