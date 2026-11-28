"""
The write-back half of the GitHub Issues task source: comments, labels, status, the factory's context section, closing.

`GitHubIssueWrites` is a mixin of `GitHubIssuesSource`, which provides `_client`; it also holds `_pages`, which both use. Each write goes to the repository
the key names, with that repository's installation token; a key of another source is only logged.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from typing import Any
from urllib.parse import quote

import httpx
from fact.core.github.app import split_full_name
from fact.core.github.client import GitHubError, GitHubNotFoundError
from fact.core.logging import get_logger
from fact.core.tasks.base import TaskStatus, parse_key
from fact.core.types import PRRef

log = get_logger(__name__)

CONTEXT_START = "<!-- fact:context:start -->"
CONTEXT_END = "<!-- fact:context:end -->"


def with_context(body: str, section: str) -> str:
    """
    Return `body` with `section` between the context markers; every byte outside them is kept as it was.

    The last start marker followed by an end marker delimits the section. Without such a pair (no markers, or broken
    ones: a lone start, a lone end, an end before the start), a fresh pair is appended and the stray markers stay, so
    text a person wrote after a lone start is never replaced. The section's own copies of the markers are dropped.
    """
    section = section.replace(CONTEXT_START, "").replace(CONTEXT_END, "").strip("\n")
    start = body.rfind(CONTEXT_START)
    end = body.find(CONTEXT_END, start + len(CONTEXT_START)) if start >= 0 else -1
    if end >= 0:
        return f"{body[: start + len(CONTEXT_START)]}\n{section}\n{body[end:]}"
    separator = "" if not body else "\n" if body.endswith("\n") else "\n\n"
    return f"{body}{separator}{CONTEXT_START}\n{section}\n{CONTEXT_END}\n"


# GitHub's native issue statuses by name (`12-integrations.md`, "Issue status"), compared case-insensitively.
NATIVE_STATUSES = {
    "backlog": TaskStatus.backlog,
    "todo": TaskStatus.todo,
    "in progress": TaskStatus.in_progress,
    "in review": TaskStatus.in_review,
    "done": TaskStatus.done,
    "not planned": TaskStatus.not_planned,
    "duplicate": TaskStatus.duplicate,
}
_STATUSES_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) { planningStatuses(first: 20) { nodes { id name } } }
}"""
_SET_STATUS = """
mutation($issueId: ID!, $statusId: ID!) {
  setIssueStatus(input: {issueId: $issueId, statusId: $statusId}) { clientMutationId }
}"""


class NoNativeStatusesError(Exception):
    """The repository has no native issue statuses, or not the one asked for."""


async def _graphql(client: httpx.AsyncClient, query: str, variables: dict[str, str]) -> dict:
    """Run a GraphQL query; GitHub answers 200 with `errors` when it fails, raised here as a `GitHubError`."""
    body = (await client.post("/graphql", json={"query": query, "variables": variables})).json()
    if errors := body.get("errors"):
        if any(e.get("type") == "NOT_FOUND" for e in errors):
            raise NoNativeStatusesError(errors[0].get("message", ""))
        raise GitHubError(f"GitHub GraphQL failed: {errors[0].get('message', '')}"[:300])
    return body.get("data") or {}


async def _status_id(client: httpx.AsyncClient, repo: str, status: TaskStatus) -> str:
    """Node id of the repository's planning status that maps to `status`."""
    owner, name = split_full_name(repo)
    data = await _graphql(client, _STATUSES_QUERY, {"owner": owner, "name": name})
    nodes = ((data.get("repository") or {}).get("planningStatuses") or {}).get("nodes") or []
    for node in nodes:
        if NATIVE_STATUSES.get(str(node.get("name", "")).lower()) == status:
            return str(node["id"])
    raise NoNativeStatusesError(status)


def _should_move(current: TaskStatus | None, target: TaskStatus) -> bool:
    """Only forward: no status moves anywhere, a status only to one further on."""
    return current is None or current.rank < target.rank


def pr_list(prs: list[PRRef]) -> str:
    lines = "\n".join(f"- {pr.repo}#{pr.number}: {pr.url}" for pr in prs)
    return f"fact opened {'this PR' if len(prs) == 1 else 'these PRs'}:\n\n{lines}"


class GitHubIssueWrites:
    _client: Callable[[str], AbstractAsyncContextManager[httpx.AsyncClient]]

    @asynccontextmanager
    async def _issue_client(self, key: str, action: str) -> AsyncIterator[tuple[httpx.AsyncClient, str] | None]:
        """
        Open a client on the key's repository; yield it with the issue's API path (`/repos/o/r/issues/n`).

        Yield None for a key that is not a GitHub issue (a file task run on a project whose source is GitHub): its
        state lives elsewhere, so the write is only logged, as the file source does.
        """
        parsed = parse_key(key)
        if parsed.kind != "gh" or parsed.repo is None:
            log.info("task_write_back_skipped", source="github", action=action, task_key=key)
            yield None
            return
        owner, name = split_full_name(parsed.repo)
        async with self._client(parsed.repo) as client:
            yield client, f"/repos/{owner}/{name}/issues/{parsed.id}"

    @staticmethod
    async def _pages(client: httpx.AsyncClient, url: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        next_url: str | None = url
        while next_url:
            response = await client.get(next_url)
            items.extend(response.json())
            next_url = response.links.get("next", {}).get("url")
        return items

    async def comment(self, key: str, text: str) -> None:
        async with self._issue_client(key, "comment") as issue:
            if issue is None:
                return
            client, path = issue
            await client.post(f"{path}/comments", json={"body": text})
        log.info("task_commented", task_key=key, chars=len(text))

    async def set_labels(self, key: str, add: list[str], remove: list[str]) -> None:
        """Add and remove labels; GitHub creates a missing label, and removing one the issue lacks is a no-op."""
        async with self._issue_client(key, "set_labels") as issue:
            if issue is None:
                return
            client, path = issue
            if add:
                await client.post(f"{path}/labels", json={"labels": add})
            for label in remove:
                with suppress(GitHubNotFoundError):  # the issue does not have it
                    await client.delete(f"{path}/labels/{quote(label, safe='')}")
        log.info("task_labels_set", task_key=key, add=add, remove=remove)

    async def write_context(self, key: str, section: str) -> None:
        """Rewrite the body's context section from the issue as it is now (`with_context`); the rest stays."""
        async with self._issue_client(key, "write_context") as issue:
            if issue is None:
                return
            client, path = issue
            body = str((await client.get(path)).json().get("body") or "")
            updated = with_context(body, section)
            if updated != body:
                await client.patch(path, json={"body": updated})
        log.info("task_context_written", task_key=key, chars=len(section))

    async def advance_status(self, key: str, status: TaskStatus) -> None:
        """
        Move the issue's native status forward to `status` (`_should_move`); never raises.

        The status is only shown to people, so a failed write, or a repository without native statuses, only logs.
        """
        try:
            async with self._issue_client(key, "advance_status") as issue:
                if issue is None:
                    return
                client, path = issue
                await self._advance(client, path, key, status)
        except NoNativeStatusesError:
            log.info("task_status_skipped", task_key=key, status=status, reason="no_native_statuses")
        except (GitHubError, httpx.HTTPError) as exc:
            log.warning("task_status_failed", task_key=key, status=status, error=str(exc))

    @staticmethod
    async def _advance(client: httpx.AsyncClient, path: str, key: str, status: TaskStatus) -> None:
        try:
            found = (await client.get(path)).json()
        except GitHubNotFoundError as exc:
            raise GitHubError(str(exc), exc.status_code) from exc  # no such issue: a failure, not missing statuses
        if "status" not in found:
            raise NoNativeStatusesError(key)
        name = str((found["status"] or {}).get("name") or "").lower()
        current = NATIVE_STATUSES.get(name) if name else None
        if name and current is None:
            log.info("task_status_skipped", task_key=key, status=status, reason="unknown_status", current=name)
            return
        if not _should_move(current, status):
            log.info("task_status_kept", task_key=key, status=status, current=current)
            return
        repo = parse_key(key).repo
        assert repo is not None  # noqa: S101 - `_issue_client` yields only GitHub issue keys
        try:
            status_id = await _status_id(client, repo, status)
        except GitHubNotFoundError as exc:
            raise NoNativeStatusesError(key) from exc
        await _graphql(client, _SET_STATUS, {"issueId": str(found["node_id"]), "statusId": status_id})
        log.info("task_status_advanced", task_key=key, status=status, previous=current)

    async def link_prs(self, key: str, prs: list[PRRef]) -> None:
        if prs:
            await self.comment(key, pr_list(prs))

    async def close(self, key: str) -> None:
        """Close the issue as completed, so tasks that depend on it count it done."""
        async with self._issue_client(key, "close") as issue:
            if issue is None:
                return
            client, path = issue
            await client.patch(path, json={"state": "closed", "state_reason": "completed"})
        log.info("task_closed", task_key=key)
