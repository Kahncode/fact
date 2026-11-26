"""The GitHub Issues task source against a fake GitHub (`httpx.MockTransport`): listing, parsing, dependencies."""

from pathlib import Path

import httpx
import pytest
from fact.core.github import GitHubError
from fact.core.tasks import GitHubIssuesSource, ReadyItem, TaskSource, branch_name
from fact.core.tasks.github_issues import acceptance_criteria, complexity, depends_on_lines, task_type
from fake_github_issues import REPO, FakeApp, FakeGitHub
from fake_github_issues import closed as _closed
from fake_github_issues import issue as _issue
from fake_github_issues import source as _source

BODY = """## Goal

Do the thing.

Depends on #3, acme/lib#4 and #2

### Acceptance criteria

- [ ] `uv run pytest` passes.
- [x] It works.

## Notes

- [ ] not a criterion
"""


def test_parsing_helpers() -> None:
    assert acceptance_criteria(BODY) == ["`uv run pytest` passes.", "It works."]
    assert depends_on_lines(BODY, REPO) == ["gh:acme/app#3", "gh:acme/lib#4", "gh:acme/app#2"]
    assert depends_on_lines("- **Depends on:** #7\nnot depends on #8", REPO) == ["gh:acme/app#7"]
    assert task_type(["type:chore"]) == "chore"
    assert task_type(["Type:Incident"]) == "bug"
    assert task_type(["type:weird"]) == task_type([]) == "feature"
    assert complexity(["complexity:complex"]) == "complex"
    assert complexity([]) == "standard"


async def test_get_reads_an_issue() -> None:
    github = FakeGitHub()
    github.add(_issue(12, labels=("fact:ready", "type:bug", "complexity:trivial"), body=BODY.replace("\n", "\r\n")))
    github.blocked_by[12] = [_closed(5), _closed(4, repo="acme/lib")]
    source: TaskSource = _source(github)
    task = await source.get("gh:acme/app#12")
    assert task is not None
    assert (task.key, task.id, task.title, task.type, task.complexity) == (
        "gh:acme/app#12",
        "12",
        "Issue 12",
        "bug",
        "trivial",
    )
    assert "\r" not in task.body
    assert task.acceptance_criteria == ["`uv run pytest` passes.", "It works."]
    # blocked-by links first, then `Depends on` lines, without repeats
    assert task.depends_on == ["gh:acme/app#5", "gh:acme/lib#4", "gh:acme/app#3", "gh:acme/app#2"]
    assert task.path is None
    assert branch_name(task.key) == "fact/gh-12"
    auth = {r.headers["Authorization"] for r in github.requests}
    assert auth == {"Bearer tok-acme/app"}


async def test_get_returns_none_for_unknown_pr_and_foreign_keys() -> None:
    github = FakeGitHub()
    github.add(_issue(7, pull_request={"url": "x"}))
    source = _source(github)
    assert await source.get("gh:acme/app#99") is None
    assert await source.get("gh:acme/app#7") is None
    assert await source.get("gh:acme/other#1") is None
    assert await source.get("task:M1-01") is None
    assert all("other" not in str(r.url) for r in github.requests)


@pytest.mark.parametrize(
    ("dep", "done"),
    [
        (_closed(3), True),
        (_closed(3, reason="not_planned"), False),
        (_closed(3, reason="duplicate"), False),
        (_issue(3), False),
        (_issue(3, state="closed"), True),  # closed before GitHub recorded a reason
        (_issue(3, pull_request={"merged_at": "2026-01-01T00:00:00Z"}), True),
        (_issue(3, state="closed", pull_request={"merged_at": None}), False),
        (None, False),
    ],
)
async def test_deps_done_from_depends_on_lines(dep: dict | None, *, done: bool) -> None:
    github = FakeGitHub()
    github.add(_issue(12, body="Depends on #3"))
    if dep is not None:
        github.add(dep)
    source = _source(github)
    task = await source.get("gh:acme/app#12")
    assert task is not None
    assert task.depends_on == ["gh:acme/app#3"]
    assert await source.deps_done(task) is done


async def test_deps_done_from_blocked_by_uses_the_listed_state() -> None:
    github = FakeGitHub()
    github.add(_issue(12))
    github.blocked_by[12] = [_closed(4, repo="acme/lib"), _issue(6)]
    source = _source(github)
    task = await source.get("gh:acme/app#12")
    assert task is not None
    assert not await source.deps_done(task)
    assert await source.deps_done(task.model_copy(update={"depends_on": ["gh:acme/lib#4"]}))
    # the states came with the blocked-by listing: no lookup of the blockers, no token for acme/lib
    assert not any(r.url.path.endswith(("/issues/4", "/issues/6")) for r in github.requests)


async def test_deps_done_reads_another_repository_with_its_token() -> None:
    github = FakeGitHub()
    github.add(_issue(12, body="Depends on acme/lib#4"))
    github.add(_closed(4, repo="acme/lib"), repo="acme/lib")
    app = FakeApp(github)
    source = GitHubIssuesSource(REPO, app)  # type: ignore[arg-type]
    task = await source.get("gh:acme/app#12")
    assert task is not None
    assert await source.deps_done(task)
    assert "acme/lib" in app.token_repos


async def test_list_ready() -> None:
    github = FakeGitHub()
    github.add(_issue(1, labels=("fact:ready",)))  # no dependencies
    github.add(_issue(2, labels=("fact:ready",), body="Depends on #9"))  # #9 still open
    github.add(_issue(3, labels=("fact:ready", "type:chore")))  # its blocker is done
    github.add(_issue(4, labels=("fact:ready",)))  # its blocker was closed as not planned
    github.add(_issue(5, labels=("fact:ready",), pull_request={"url": "x"}))  # a PR, never a task
    github.add(_issue(6))  # not ready
    github.add(_issue(9))
    github.blocked_by[3] = [_closed(8)]
    github.blocked_by[4] = [_closed(7, reason="not_planned")]
    ready = await _source(github).list_ready()
    assert all(isinstance(r, ReadyItem) and r.depth == 0 for r in ready)
    assert [(r.task.key, r.task.type) for r in ready] == [("gh:acme/app#1", "feature"), ("gh:acme/app#3", "chore")]
    assert any(r.url.params.get("page") == "2" for r in github.requests)  # followed the Link header


async def test_api_errors_raise() -> None:
    github = FakeGitHub()
    source = _source(github)
    github.handle = lambda _request: httpx.Response(500, json={"message": "boom"})  # type: ignore[method-assign]
    with pytest.raises(GitHubError):
        await source.list_ready()


def test_mark_done_leaves_the_branch_alone(tmp_path: Path) -> None:
    assert _source(FakeGitHub()).mark_done(tmp_path, "gh:acme/app#1") is None  # the issue closes on merge instead


def test_rejects_a_bad_repo() -> None:
    with pytest.raises(ValueError, match="owner/repo"):
        GitHubIssuesSource("not-a-repo", FakeApp(FakeGitHub()))  # type: ignore[arg-type]


async def test_is_closed_for_any_reason() -> None:
    github = FakeGitHub()
    github.add(_issue(1))
    github.add(_closed(2))
    github.add(_closed(3, reason="not_planned"))
    source = _source(github)
    assert await source.is_closed("gh:acme/app#1") is False
    assert await source.is_closed("gh:acme/app#2") is True
    assert await source.is_closed("gh:acme/app#3") is True
    assert await source.is_closed("gh:acme/app#99") is True  # gone
