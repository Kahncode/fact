"""A stateful fake of the GitHub API for the push and PR steps: tokens, the repository, its branches and its PRs."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fact.core.github import GitHubApp

FULL_NAME = "acme/widget"
READ_TOKEN = "ghs_changeset_read_41"  # noqa: S105 - fake
WRITE_TOKEN = "ghs_changeset_write_42"  # noqa: S105 - fake
BOT_ID = 4321
PR_NUMBER = 12
BRANCH_REF = f"/repos/{FULL_NAME}/git/ref/heads/"


def private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


@dataclass
class FakeGitHub:
    """`branch_sha` reads a branch's head from the bare repository standing in for GitHub's; None when it has none."""

    branch_sha: Callable[[str], str | None]
    open_prs: list[dict] = field(default_factory=list)
    requests: list[tuple[str, str, dict | None]] = field(default_factory=list)
    reject_create: bool = False  # answer 422 like GitHub does for a branch with no commits

    def app(self, key: str) -> GitHubApp:
        return GitHubApp(1, key, api_url="https://gh.test", transport=httpx.MockTransport(self.handle))

    def permissions_asked(self) -> list[dict]:
        """Return the `permissions` of each token minted, in order (`{}`: all of the installation's)."""
        return [b.get("permissions", {}) for _m, p, b in self.requests if p.endswith("/access_tokens") and b]

    def posted_prs(self) -> list[dict]:
        return [b for m, p, b in self.requests if m == "POST" and p.endswith("/pulls") and b]

    async def handle(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        self.requests.append((method, path, body))
        if path == "/app":
            return httpx.Response(200, json={"id": 1, "slug": "fact", "name": "fact"})
        if path == "/users/fact[bot]":
            return httpx.Response(200, json={"id": BOT_ID})
        if path == f"/repos/{FULL_NAME}/installation":
            return httpx.Response(200, json={"id": 7, "account": {"login": "acme"}})
        if path == "/app/installations/7/access_tokens":
            write = (body or {}).get("permissions", {}).get("contents") == "write"
            token = WRITE_TOKEN if write else READ_TOKEN
            return httpx.Response(201, json={"token": token, "expires_at": "2099-01-01T00:00:00Z"})
        if path == f"/repos/{FULL_NAME}":
            return httpx.Response(200, json={"default_branch": "main", "html_url": f"https://github.com/{FULL_NAME}"})
        if path.startswith(BRANCH_REF) and method == "GET":
            sha = self.branch_sha(path.removeprefix(BRANCH_REF))
            if sha is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"object": {"sha": sha, "type": "commit"}})
        return self._pulls(path, method, request)

    def _pulls(self, path: str, method: str, request: httpx.Request) -> httpx.Response:
        if path == f"/repos/{FULL_NAME}/pulls" and method == "GET":
            assert request.url.params["base"] == "main"
            head = request.url.params["head"].removeprefix("acme:")
            return httpx.Response(200, json=[pr for pr in self.open_prs if pr["head"] == head])
        if path == f"/repos/{FULL_NAME}/pulls" and method == "POST" and self.reject_create:
            return httpx.Response(422, json={"message": "No commits between main and the branch"})
        if path == f"/repos/{FULL_NAME}/pulls" and method == "POST":
            head = json.loads(request.content)["head"]
            pr = {"number": PR_NUMBER, "html_url": f"https://github.com/{FULL_NAME}/pull/12", "head": head}
            self.open_prs.append(pr)
            return httpx.Response(201, json=pr)
        if path == f"/repos/{FULL_NAME}/pulls/{PR_NUMBER}" and method == "PATCH":
            return httpx.Response(200, json=self.open_prs[0])
        return httpx.Response(404, json={"message": "Not Found"})
