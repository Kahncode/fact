"""Shared by the `gh:` API tests: project `issues` whose task source is GitHub Issues on `acme/app`, and a fake GitHub."""

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from fact.core.github import github_client
from task_repo import write

PROJECT = "issues"
PROJECT_YAML = """name: issues
repositories:
  - full_name: acme/app
    role: app
task_source:
  kind: github
  repo: acme/app
workflows:
  default: standard
  bug: bugfix
"""
_READY = [{"name": "fact:ready"}, {"name": "type:bug"}]
ISSUES = {
    12: {"number": 12, "title": "Fix it", "body": "Depends on #3\n", "labels": _READY},
    13: {"number": 13, "title": "Then this", "body": "Depends on #12\n", "labels": _READY},
    3: {"number": 3, "title": "Done", "body": "", "state": "closed", "state_reason": "completed", "labels": []},
    14: {"number": 14, "title": "Held", "body": "", "labels": [*_READY, {"name": "fact:needs-human"}]},
    20: {"number": 20, "title": "A plan", "body": "", "labels": [{"name": "fact:planned"}]},
    21: {"number": 21, "title": "Its step", "body": "", "labels": _READY},
}
PARENTS = {21: 20}  # sub-issue -> parent


@dataclass
class FakeIssues:
    """`ISSUES` as GitHub serves them; records each write as (method, path) and keeps labels as GitHub would."""

    issues: dict[int, dict] = field(default_factory=lambda: copy.deepcopy(ISSUES))
    writes: list[tuple[str, str]] = field(default_factory=list)  # shared with whatever else wants the order
    parents: dict[int, int] = field(default_factory=lambda: dict(PARENTS))
    blocked_by: dict[int, list[int]] = field(default_factory=dict)  # issue -> the issues blocking it

    def __post_init__(self) -> None:  # noqa: D105
        for issue in self.issues.values():
            issue.setdefault("state", "open")
            issue.setdefault("state_reason", None)

    def labels(self, n: int) -> set[str]:
        return {label["name"] for label in self.issues[n]["labels"]}

    def handle(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")
        if request.method != "GET":
            return self._write(request, parts)
        if request.url.path == "/repos/acme/app/issues":
            ready = [
                i for i in self.issues.values() if i["state"] == "open" and "fact:ready" in self.labels(i["number"])
            ]
            return httpx.Response(200, json=ready)
        if parts[-2:] == ["dependencies", "blocked_by"]:
            return httpx.Response(200, json=[self.issues[n] for n in self.blocked_by.get(int(parts[-3]), [])])
        if parts[-1] == "sub_issues":
            return httpx.Response(200, json=[self.issues[c] for c, p in self.parents.items() if p == int(parts[-2])])
        if parts[-1] == "parent":
            parent = self.parents.get(int(parts[-2]))
            return self._found(self.issues[parent] if parent else None)
        return self._found(self.issues.get(int(parts[-1])) if parts[1:3] == ["acme", "app"] else None)

    def _write(self, request: httpx.Request, parts: list[str]) -> httpx.Response:
        self.writes.append((request.method, request.url.path))
        issue = self.issues[int(parts[4])]
        if request.method == "POST" and parts[-1] == "labels":
            added = json.loads(request.content)["labels"]
            issue["labels"] = [*issue["labels"], *({"name": n} for n in added if n not in self.labels(issue["number"]))]
        elif request.method == "DELETE":
            if parts[-1] not in self.labels(issue["number"]):
                return httpx.Response(404, json={"message": "Label does not exist"})
            issue["labels"] = [label for label in issue["labels"] if label["name"] != parts[-1]]
        return httpx.Response(200, json={})

    @staticmethod
    def _found(found: dict | None) -> httpx.Response:
        return httpx.Response(200, json=found) if found else httpx.Response(404, json={"message": "Not Found"})


@dataclass(frozen=True)
class _Token:
    token: str


@dataclass
class FakeApp:
    github: FakeIssues

    async def installation_token(self, full_name: str) -> _Token:
        return _Token(f"tok-{full_name}")

    def api_client(self, token: str | None) -> httpx.AsyncClient:
        return github_client(token, api_url="https://gh.test", transport=httpx.MockTransport(self.github.handle))


def write_issues_config(tmp_path: Path) -> Path:
    home = tmp_path / "home"  # a GitHub source reads no task files: no git needed
    write(home, {".fact/project.yaml": PROJECT_YAML})
    config = tmp_path / "fact.yaml"
    config.write_text(
        f"data_dir: {(tmp_path / 'data').as_posix()}\n"
        f"projects:\n  - name: {PROJECT}\n    home: acme/app\n    path: {home.as_posix()}\n",
        encoding="utf-8",
    )
    return config
