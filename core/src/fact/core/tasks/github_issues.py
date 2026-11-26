"""
GitHub Issues task source: open issues labelled `fact:ready` in one repository are tasks (`gh:owner/repo#12`).

Reading only (M2-09): the write-back methods log, like the file source's, until M2-11 writes to issues.

- Type and complexity come from the `type:*` and `complexity:*` labels; acceptance criteria from the checklist under
  an "Acceptance criteria" heading.
- Dependencies come from GitHub's blocked-by links and from `Depends on #n` lines in the body; a dependency is done
  when its issue is closed as completed.
"""

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, get_args

import httpx
from fact.core.github.app import GitHubApp, split_full_name
from fact.core.github.client import GitHubNotFoundError
from fact.core.logging import get_logger
from fact.core.tasks.base import GITHUB_PREFIX, ReadyItem, dependency_key, parse_key
from fact.core.types import Complexity, PRRef, Task, TaskType

log = get_logger(__name__)

READY_LABEL = "fact:ready"
PER_PAGE = 100
TYPE_ALIASES: dict[str, TaskType] = {"incident": "bug"}  # an incident runs the bug workflow
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_CRITERION_RE = re.compile(r"^\s*[-*+]\s*\[[ xX]\]\s*(.+?)\s*$")
_DEPENDS_RE = re.compile(r"^\s*(?:[-*+]\s*)?\**depends on\**:?\**\s*(.+)$", re.IGNORECASE)
_REF_RE = re.compile(r"(?<![\w/.-])(?:([A-Za-z0-9-]+/[A-Za-z0-9._-]+))?#([1-9]\d*)\b")


def acceptance_criteria(body: str) -> list[str]:
    """Checklist items under an "Acceptance criteria" heading of any level, up to the next heading."""
    found: list[str] = []
    in_section = False
    for line in body.splitlines():
        if m := _HEADING_RE.match(line):
            in_section = m.group(1).strip().rstrip(":").lower() == "acceptance criteria"
        elif in_section and (m := _CRITERION_RE.match(line)):
            found.append(m.group(1))
    return found


def depends_on_lines(body: str, repo: str) -> list[str]:
    """Keys from `Depends on #3, owner/other#4` lines; a bare `#n` is an issue of `repo`."""
    keys: list[str] = []
    for line in body.splitlines():
        if m := _DEPENDS_RE.match(line):
            keys.extend(f"{GITHUB_PREFIX}{r or repo}#{n}" for r, n in _REF_RE.findall(m.group(1)))
    return keys


def _label_names(issue: dict[str, Any]) -> list[str]:
    return [str(label["name"] if isinstance(label, dict) else label) for label in issue.get("labels") or []]


def _label_value(labels: list[str], group: str) -> str | None:
    prefix = f"{group}:"
    return next((n[len(prefix) :].strip().lower() for n in labels if n.lower().startswith(prefix)), None)


def task_type(labels: list[str]) -> TaskType:
    value = _label_value(labels, "type") or "feature"
    value = TYPE_ALIASES.get(value, value)
    return value if value in get_args(TaskType) else "feature"  # type: ignore[return-value]


def complexity(labels: list[str]) -> Complexity:
    value = _label_value(labels, "complexity")
    return value if value in get_args(Complexity) else "standard"  # type: ignore[return-value]


def is_done(issue: dict[str, Any]) -> bool:
    """
    Tell whether the issue is closed as completed; one closed as not planned or duplicate is not done.

    A closed issue without a reason (closed before GitHub recorded one) is done; a PR is done once merged.
    """
    if (pr := issue.get("pull_request")) is not None:
        return bool(pr.get("merged_at"))
    return issue.get("state") == "closed" and issue.get("state_reason") in {"completed", None}


def _issue_key(issue: dict[str, Any], default_repo: str) -> str:
    repo = (issue.get("repository") or {}).get("full_name")
    if not repo and (url := issue.get("repository_url")):
        repo = "/".join(str(url).rstrip("/").split("/")[-2:])
    return f"{GITHUB_PREFIX}{repo or default_repo}#{issue['number']}"


def _unique(keys: list[str]) -> list[str]:
    """Drop repeated keys (GitHub names are case-insensitive), keeping the first spelling and the order."""
    out: list[str] = []
    seen: set[str] = set()
    for key in keys:
        if key.lower() not in seen:
            seen.add(key.lower())
            out.append(key)
    return out


class GitHubIssuesSource:
    """
    Issues of `repo` ("owner/repo") read with the App's installation token. Implements `TaskSource`.

    Issue states fetched by one instance are reused by it: keep an instance for one read (one request or one tick).
    """

    def __init__(self, repo: str, app: GitHubApp) -> None:
        split_full_name(repo)
        self.repo = repo
        self.app = app
        self._issues: dict[str, dict[str, Any]] = {}  # lower-cased key -> issue, as fetched

    @asynccontextmanager
    async def _client(self, repo: str) -> AsyncIterator[httpx.AsyncClient]:
        token = await self.app.installation_token(repo)
        async with self.app.api_client(token.token) as client:
            yield client

    @staticmethod
    async def _pages(client: httpx.AsyncClient, url: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        next_url: str | None = url
        while next_url:
            response = await client.get(next_url)
            items.extend(response.json())
            next_url = response.links.get("next", {}).get("url")
        return items

    def _remember(self, issue: dict[str, Any], repo: str) -> None:
        self._issues[_issue_key(issue, repo).lower()] = issue

    async def _issue(self, key: str) -> dict[str, Any] | None:
        """Fetch the issue (or PR) behind `key`, fetched once per instance; None if GitHub has none."""
        if (cached := self._issues.get(key.lower())) is not None:
            return cached
        parsed = parse_key(key)
        assert parsed.repo is not None  # noqa: S101 - a gh key always has one
        owner, name = split_full_name(parsed.repo)
        try:
            async with self._client(parsed.repo) as client:
                issue = (await client.get(f"/repos/{owner}/{name}/issues/{parsed.id}")).json()
        except GitHubNotFoundError:  # no such issue, or the App is not installed on its repository
            log.warning("task_issue_unreadable", task_key=key)
            return None
        self._issues[key.lower()] = issue
        return issue

    async def _blocked_by(self, client: httpx.AsyncClient, number: int) -> list[str]:
        owner, name = split_full_name(self.repo)
        url = f"/repos/{owner}/{name}/issues/{number}/dependencies/blocked_by?per_page={PER_PAGE}"
        blockers = await self._pages(client, url)
        for issue in blockers:
            self._remember(issue, self.repo)
        return [_issue_key(issue, self.repo) for issue in blockers]

    async def _task(self, client: httpx.AsyncClient, issue: dict[str, Any]) -> Task:
        body = str(issue.get("body") or "").replace("\r\n", "\n")
        labels = _label_names(issue)
        number = int(issue["number"])
        deps = [*await self._blocked_by(client, number), *depends_on_lines(body, self.repo)]
        own = f"{GITHUB_PREFIX}{self.repo}#{number}"
        return Task(
            key=own,
            id=str(number),
            title=str(issue.get("title") or ""),
            body=body,
            acceptance_criteria=acceptance_criteria(body),
            type=task_type(labels),
            complexity=complexity(labels),
            depends_on=[d for d in _unique(deps) if d.lower() != own.lower()],
        )

    def _owns(self, key: str) -> bool:
        try:
            parsed = parse_key(key)
        except ValueError:
            return False
        return parsed.kind == "gh" and (parsed.repo or "").lower() == self.repo.lower()

    async def get(self, key: str) -> Task | None:
        """Read issue `key` of this repository, open or closed; None for a PR or another source's key."""
        if not self._owns(key):
            return None
        issue = await self._issue(key)
        if issue is None or "pull_request" in issue:
            return None
        async with self._client(self.repo) as client:
            return await self._task(client, issue)

    async def deps_done(self, task: Task) -> bool:
        for dep in task.depends_on:
            issue = await self._issue(dependency_key(task, dep))
            if issue is None or not is_done(issue):
                return False
        return True

    async def list_ready(self) -> list[ReadyItem]:
        """Open `fact:ready` issues (not PRs) whose dependencies are all done; their dependencies are closed: depth 0."""
        owner, name = split_full_name(self.repo)
        url = f"/repos/{owner}/{name}/issues?state=open&labels={READY_LABEL}&per_page={PER_PAGE}"
        async with self._client(self.repo) as client:
            issues = [i for i in await self._pages(client, url) if "pull_request" not in i]
            for issue in issues:
                self._remember(issue, self.repo)
            tasks = [await self._task(client, issue) for issue in issues]
        return [ReadyItem(task) for task in tasks if await self.deps_done(task)]

    def mark_done(self, worktree: Path, key: str) -> str | None:
        """None: an issue is closed when its change set merges, not by a commit."""
        del worktree, key
        return None

    async def comment(self, key: str, text: str) -> None:
        log.info("task_write_back_skipped", source="github", action="comment", task_key=key, chars=len(text))

    async def set_labels(self, key: str, add: list[str], remove: list[str]) -> None:
        log.info("task_write_back_skipped", source="github", action="set_labels", task_key=key, add=add, remove=remove)

    async def write_context(self, key: str, section: str) -> None:
        log.info("task_write_back_skipped", source="github", action="write_context", task_key=key, chars=len(section))

    async def link_prs(self, key: str, prs: list[PRRef]) -> None:
        urls = [pr.url for pr in prs]
        log.info("task_write_back_skipped", source="github", action="link_prs", task_key=key, prs=urls)

    async def close(self, key: str) -> None:
        log.info("task_write_back_skipped", source="github", action="close", task_key=key)
