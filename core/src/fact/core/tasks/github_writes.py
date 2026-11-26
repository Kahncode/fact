"""
The write-back half of the GitHub Issues task source: comments, labels, the factory's context section, closing.

`GitHubIssueWrites` is a mixin of `GitHubIssuesSource`, which provides `_client`. Each write goes to the repository
the key names, with that repository's installation token; a key of another source is only logged.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from urllib.parse import quote

import httpx
from fact.core.github.app import split_full_name
from fact.core.github.client import GitHubNotFoundError
from fact.core.logging import get_logger
from fact.core.tasks.base import parse_key
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
