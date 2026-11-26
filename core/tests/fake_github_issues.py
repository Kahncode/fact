"""A fake GitHub (`httpx.MockTransport`) serving issues, blocked-by links, parents and sub-issues, for the core tests."""

import json
from dataclasses import dataclass, field

import httpx
from fact.core.github import github_client
from fact.core.tasks import GitHubIssuesSource

REPO = "acme/app"


def issue(
    n: int, *, labels: tuple[str, ...] = (), state: str = "open", reason: str | None = None, **extra: object
) -> dict:
    return {
        "number": n,
        "title": f"Issue {n}",
        "body": extra.pop("body", ""),
        "state": state,
        "state_reason": reason,
        "labels": [{"name": name} for name in labels],
        "repository_url": f"https://api.github.com/repos/{extra.pop('repo', REPO)}",
        **extra,
    }


def closed(n: int, reason: str = "completed", repo: str = REPO, **extra: object) -> dict:
    return issue(n, state="closed", reason=reason, repo=repo, **extra)


@dataclass
class FakeGitHub:
    """
    Serves issues by (repo, number), blocked-by links by number, and the `fact:ready` listing in two pages.

    `parents` maps a child's (repo, number) to its parent's; `/parent` and `/sub_issues` are read from it. Comments are
    recorded in `comments`, and a PATCH of an issue updates it.
    """

    issues: dict[tuple[str, int], dict] = field(default_factory=dict)
    blocked_by: dict[int, list[dict]] = field(default_factory=dict)
    parents: dict[tuple[str, int], tuple[str, int]] = field(default_factory=dict)
    comments: list[tuple[int, str]] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)

    def add(self, issue: dict, repo: str = REPO, parent: int | None = None) -> None:
        self.issues[repo, issue["number"]] = issue
        if parent is not None:
            self.parents[repo, issue["number"]] = (REPO, parent)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        parts = request.url.path.strip("/").split("/")  # repos, owner, repo, issues, ...
        repo = f"{parts[1]}/{parts[2]}"
        rest = parts[4:]
        if not rest:
            return self._listing(request, repo)
        number = int(rest[0])
        if rest[1:] == ["dependencies", "blocked_by"]:
            return httpx.Response(200, json=self.blocked_by.get(number, []))
        if rest[1:] == ["parent"]:
            parent = self.parents.get((repo, number))
            return self._found(self.issues.get(parent) if parent else None)
        if rest[1:] == ["sub_issues"]:
            subs = [self.issues[child] for child, parent in self.parents.items() if parent == (repo, number)]
            return httpx.Response(200, json=subs)
        if rest[1:] == ["comments"] and request.method == "POST":
            self.comments.append((number, json.loads(request.content)["body"]))
            return httpx.Response(201, json={})
        found = self.issues.get((repo, number))
        if found is not None and request.method == "PATCH":
            found.update(json.loads(request.content))
        return self._found(found)

    @staticmethod
    def _found(found: dict | None) -> httpx.Response:
        return httpx.Response(200, json=found) if found else httpx.Response(404, json={"message": "Not Found"})

    def _listing(self, request: httpx.Request, repo: str) -> httpx.Response:
        assert request.url.params["state"] == "open"
        assert request.url.params["labels"] == "fact:ready"
        ready = [
            i
            for (r, _), i in sorted(self.issues.items())
            if r == repo and i["state"] == "open" and "fact:ready" in [x["name"] for x in i["labels"]]
        ]
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=ready[1:])
        next_url = f"https://gh.test/repos/{repo}/issues?state=open&labels=fact:ready&per_page=100&page=2"
        return httpx.Response(200, json=ready[:1], headers={"Link": f'<{next_url}>; rel="next"'})

    def paths(self, suffix: str) -> list[str]:
        """Paths of the requests made so far that end with `suffix`."""
        return [r.url.path for r in self.requests if r.url.path.endswith(suffix)]


@dataclass(frozen=True)
class _Token:
    token: str


@dataclass
class FakeApp:
    """Stands in for `GitHubApp`: one token per repository, clients on the fake transport."""

    github: FakeGitHub
    token_repos: list[str] = field(default_factory=list)

    async def installation_token(self, full_name: str) -> _Token:
        self.token_repos.append(full_name)
        return _Token(f"tok-{full_name}")

    def api_client(self, token: str | None) -> httpx.AsyncClient:
        return github_client(token, api_url="https://gh.test", transport=httpx.MockTransport(self.github.handle))


def source(github: FakeGitHub) -> GitHubIssuesSource:
    return GitHubIssuesSource(REPO, FakeApp(github))  # type: ignore[arg-type]
