"""Thin `httpx.AsyncClient` factory for the GitHub REST API, with readable errors for 401, 403 and 404."""

import httpx

API_VERSION = "2022-11-28"
DEFAULT_TIMEOUT_S = 30.0
_MESSAGE_LIMIT = 300


class GitHubError(Exception):
    """A GitHub API call failed. `status_code` is the HTTP status; the message never carries credentials."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubAuthError(GitHubError):
    """401: the App JWT or installation token was rejected."""


class GitHubForbiddenError(GitHubError):
    """403: missing permission, or a rate limit."""


class GitHubNotFoundError(GitHubError):
    """404: the resource does not exist, or the App cannot see it."""


_HINTS: dict[int, tuple[type[GitHubError], str]] = {
    401: (GitHubAuthError, "GitHub rejected the credentials; check the App ID, the private key and the clock"),
    403: (GitHubForbiddenError, "GitHub refused the request; the App lacks a permission, or a rate limit was hit"),
    404: (GitHubNotFoundError, "GitHub found nothing; check the name and that the App is installed on it"),
}


def _github_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:_MESSAGE_LIMIT]
    message = body.get("message") if isinstance(body, dict) else None
    return str(message)[:_MESSAGE_LIMIT] if message else ""


def error_for(response: httpx.Response) -> GitHubError:
    """Build the exception for an error response. Uses the method, URL and GitHub's message, never headers."""
    request = response.request
    cls, hint = _HINTS.get(response.status_code, (GitHubError, "GitHub request failed"))
    detail = _github_message(response)
    message = f"{hint} ({response.status_code} on {request.method} {request.url.path})"
    if detail:
        message += f": {detail}"
    return cls(message, response.status_code)


async def _raise_on_error(response: httpx.Response) -> None:
    if response.is_error:
        await response.aread()
        raise error_for(response)


def github_client(
    token: str | None,
    *,
    api_url: str,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> httpx.AsyncClient:
    """
    Async client rooted at `api_url`, sending `token` (App JWT or installation token) as a bearer token.

    `token=None` makes unauthenticated calls. Any 4xx or 5xx response raises a `GitHubError` subclass.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "fact",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.AsyncClient(
        base_url=api_url.rstrip("/"),
        headers=headers,
        transport=transport,
        timeout=timeout,
        follow_redirects=True,  # renamed repositories answer 301; httpx drops auth on cross-origin hops
        event_hooks={"response": [_raise_on_error]},
    )
