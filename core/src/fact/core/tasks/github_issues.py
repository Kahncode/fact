"""
GitHub Issues task source: open issues labelled `fact:ready` in one repository are tasks (`gh:owner/repo#12`).

Write-backs (labels, comments, the context section between markers in the body, closing) are in `github_writes`; the
issue's fields (type, complexity, priority, acceptance criteria, `Depends on` lines) are read in `github_fields`.

- Dependencies come from GitHub's blocked-by links, from `Depends on #n` lines in the body and from the issue's open
  sub-issues. A blocked-by or `Depends on` dependency is done when its issue is closed as completed; a sub-issue
  closed for any reason is out of its parent's way, so only open ones are listed.
- A parent's label gates its sub-issues, transitively: an issue is ready only when it and every ancestor carry
  `fact:ready` (`not_ready_ancestor`).
- An epic (`type:epic`) never runs: once it is ready with nothing to wait on, `list_ready(close_epics=True)` comments
  and closes it as completed.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fact.core.github import GitHubError
from fact.core.github.app import GitHubApp, split_full_name
from fact.core.github.client import GitHubNotFoundError
from fact.core.logging import get_logger
from fact.core.tasks.base import GITHUB_PREFIX, ReadyItem, dependency_key, parse_key
from fact.core.tasks.github_fields import (
    READY_LABEL,
    acceptance_criteria,
    complexity,
    depends_on_lines,
    has_label,
    is_done,
    is_epic,
    issue_key,
    label_names,
    priority,
    task_type,
    unique_keys,
)
from fact.core.tasks.github_writes import GitHubIssueWrites
from fact.core.types import Task

log = get_logger(__name__)

__all__ = ["READY_LABEL", "GitHubIssuesSource", "acceptance_criteria", "complexity", "depends_on_lines", "task_type"]

PER_PAGE = 100
MAX_ANCESTORS = 16  # GitHub nests sub-issues 8 levels deep; the bound only guards against a cycle
EPIC_CLOSED = (
    "fact closed this epic as completed: it is `fact:ready` and none of its sub-issues is open. "
    "An epic has no work of its own, so it never starts a run."
)


class GitHubIssuesSource(GitHubIssueWrites):
    """
    Issues of `repo` ("owner/repo") read and written with the App's installation token. Implements `TaskSource`.

    Issue states and parent links fetched by one instance are reused by it: keep an instance for one read (one request
    or one scheduler tick), which then asks GitHub for each issue's parent at most once.
    """

    def __init__(self, repo: str, app: GitHubApp) -> None:
        split_full_name(repo)
        self.repo = repo
        self.app = app
        self._issues: dict[str, dict[str, Any]] = {}  # lower-cased key -> issue, as fetched
        self._parents: dict[str, str | None] = {}  # lower-cased key -> its parent's key; None: a top-level issue

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

    def _remember(self, issue: dict[str, Any], repo: str) -> str:
        key = issue_key(issue, repo)
        self._issues[key.lower()] = issue
        return key

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

    async def _parent(self, key: str) -> str | None:
        """Key of issue `key`'s parent (whose issue is remembered), asked once per instance; None without one."""
        if key.lower() in self._parents:
            return self._parents[key.lower()]
        parsed = parse_key(key)
        assert parsed.repo is not None  # noqa: S101 - a gh key always has one
        owner, name = split_full_name(parsed.repo)
        parent: str | None = None
        try:
            async with self._client(parsed.repo) as client:
                parent = self._remember(
                    (await client.get(f"/repos/{owner}/{name}/issues/{parsed.id}/parent")).json(), parsed.repo
                )
        except GitHubNotFoundError:  # a top-level issue, or one on a repository the App cannot read
            pass
        self._parents[key.lower()] = parent
        return parent

    async def not_ready_ancestor(self, key: str) -> str | None:
        """Return the nearest ancestor of issue `key` without `fact:ready`; None when all have it, or for another key."""
        if parse_key(key).kind != "gh":
            return None
        seen = {key.lower()}
        current = key
        for _ in range(MAX_ANCESTORS):
            parent = await self._parent(current)
            if parent is None or parent.lower() in seen:
                return None
            seen.add(parent.lower())
            if not has_label(self._issues.get(parent.lower(), {}), READY_LABEL):
                return parent
            current = parent
        return None

    async def _blocked_by(self, client: httpx.AsyncClient, number: int) -> list[str]:
        owner, name = split_full_name(self.repo)
        url = f"/repos/{owner}/{name}/issues/{number}/dependencies/blocked_by?per_page={PER_PAGE}"
        return [self._remember(issue, self.repo) for issue in await self._pages(client, url)]

    async def _open_sub_issues(self, client: httpx.AsyncClient, issue: dict[str, Any]) -> list[str]:
        """Keys of the issue's open sub-issues; each one's parent is known from here on, without asking GitHub."""
        summary = issue.get("sub_issues_summary")
        if isinstance(summary, dict) and not summary.get("total"):
            return []  # GitHub says it has none: skip the request
        owner, name = split_full_name(self.repo)
        own = issue_key(issue, self.repo)
        url = f"/repos/{owner}/{name}/issues/{issue['number']}/sub_issues?per_page={PER_PAGE}"
        keys = []
        for sub in await self._pages(client, url):
            key = self._remember(sub, self.repo)
            self._parents[key.lower()] = own
            if sub.get("state") == "open":
                keys.append(key)
        return keys

    async def _task(self, client: httpx.AsyncClient, issue: dict[str, Any]) -> Task:
        body = str(issue.get("body") or "").replace("\r\n", "\n")
        labels = label_names(issue)
        number = int(issue["number"])
        deps = [
            *await self._blocked_by(client, number),
            *depends_on_lines(body, self.repo),
            *await self._open_sub_issues(client, issue),
        ]
        own = f"{GITHUB_PREFIX}{self.repo}#{number}"
        return Task(
            key=own,
            id=str(number),
            title=str(issue.get("title") or ""),
            body=body,
            acceptance_criteria=acceptance_criteria(body),
            type=task_type(labels),
            complexity=complexity(labels),
            priority=priority(labels),
            depends_on=[d for d in unique_keys(deps) if d.lower() != own.lower()],
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

    async def is_closed(self, key: str) -> bool:
        """Whether issue `key` is closed, for any reason; an issue GitHub no longer shows counts as closed."""
        issue = await self._issue(key)
        return issue is None or issue.get("state") == "closed"

    async def deps_done(self, task: Task) -> bool:
        return not await self._waiting_on(task)

    async def _waiting_on(self, task: Task) -> tuple[str, ...]:
        """Keys of the dependencies not done, in order."""
        waiting = []
        for dep in task.depends_on:
            issue = await self._issue(key := dependency_key(task, dep))
            if issue is None or not is_done(issue):
                waiting.append(key)
        return tuple(waiting)

    async def _close_epic(self, key: str) -> None:
        try:
            await self.comment(key, EPIC_CLOSED)
            await self.close(key)
        except (GitHubError, httpx.HTTPError) as exc:  # the next tick tries again
            log.warning("epic_close_failed", task_key=key, error=str(exc))
            return
        log.info("epic_closed", task_key=key)

    async def list_ready(self, *, include_waiting: bool = False, close_epics: bool = False) -> list[ReadyItem]:
        """
        Open `fact:ready` issues (not PRs) under ready ancestors whose dependencies are done (depth 0).

        `include_waiting`: also the others, with `waiting_on` and `gated_by` set. Epics are never listed;
        `close_epics` closes those with nothing left to wait on.
        """
        owner, name = split_full_name(self.repo)
        url = f"/repos/{owner}/{name}/issues?state=open&labels={READY_LABEL}&per_page={PER_PAGE}"
        async with self._client(self.repo) as client:
            issues = [i for i in await self._pages(client, url) if "pull_request" not in i]
            for issue in issues:
                self._remember(issue, self.repo)
            tasks = [(issue, await self._task(client, issue)) for issue in issues]
        items: list[ReadyItem] = []
        for issue, task in tasks:
            item = ReadyItem(
                task, waiting_on=await self._waiting_on(task), gated_by=await self.not_ready_ancestor(task.key)
            )
            if is_epic(issue):
                if close_epics and not item.waiting_on and item.gated_by is None:
                    await self._close_epic(task.key)
            elif include_waiting or (not item.waiting_on and item.gated_by is None):
                items.append(item)
        return items

    def mark_done(self, worktree: Path, key: str) -> str | None:
        """None: an issue is closed when its change set merges, not by a commit."""
        del worktree, key
        return None
