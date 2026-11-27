"""The task source interface: key parsing, branch names, `source_for`, and the file source behind the protocol."""

from pathlib import Path

import pytest
from fact.core.github import GitHubApp, GitHubAppConfigError
from fact.core.project_config import ProjectConfig
from fact.core.settings import Settings
from fact.core.tasks import (
    GitHubIssuesSource,
    TaskKey,
    TaskSource,
    TaskStatus,
    branch_name,
    dependency_key,
    parse_key,
    source_for,
)
from fact.core.types import PRRef, Task

TASK = """---
id: {id}
title: "Title {id}"
status: {status}
depends_on: [{deps}]
---

## Goal

Do it.
"""


def _write(root: Path, task_id: str, status: str = "ready", deps: str = "") -> None:
    milestone, num = task_id.split("-")
    folder = root / "tasks" / milestone
    folder.mkdir(parents=True, exist_ok=True)
    text = TASK.format(id=task_id, status=status, deps=deps)
    (folder / f"{num}-x.md").write_text(text, encoding="utf-8", newline="\n")


def _config(kind: str = "files") -> ProjectConfig:
    source = {"kind": kind, "repo": "acme/tasks"} if kind == "github" else {"kind": kind}
    return ProjectConfig.model_validate(
        {
            "name": "p",
            "repositories": [{"full_name": "acme/app", "role": "app"}],
            "task_source": source,
            "workflows": {"default": "standard"},
        }
    )


@pytest.mark.parametrize(
    ("key", "parsed"),
    [
        ("task:M1-02", TaskKey(kind="task", id="M1-02")),
        ("gh:acme/app#12", TaskKey(kind="gh", id="12", repo="acme/app")),
        ("gh:Kahn-code/my.repo_x#7", TaskKey(kind="gh", id="7", repo="Kahn-code/my.repo_x")),
    ],
)
def test_parse_key(key: str, parsed: TaskKey) -> None:
    assert parse_key(key) == parsed


def test_github_key_number() -> None:
    assert parse_key("gh:acme/app#12").number == 12  # noqa: PLR2004
    assert parse_key("task:M1-02").number is None


@pytest.mark.parametrize(
    "key",
    [
        "",
        "M1-02",
        "task:",
        "task:../x",
        "task:M1-02\n",
        "gh:acme/app",
        "gh:acme#1",
        "gh:acme/app#0",
        "gh:acme/app#x",
        "gh:acme/..#1",
        "gh:acme/.#1",
        "lin:APP-1",
    ],
)
def test_parse_key_rejects(key: str) -> None:
    with pytest.raises(ValueError, match="invalid task key"):
        parse_key(key)


def test_branch_name() -> None:
    assert branch_name("task:M1-02") == "fact/task-m1-02"
    assert branch_name("gh:acme/app#12") == "fact/gh-12"


def test_dependency_key() -> None:
    file_task = Task(key="task:M1-02", id="M1-02", title="t")
    issue = Task(key="gh:acme/app#12", id="12", title="t")
    assert dependency_key(file_task, "M1-01") == "task:M1-01"
    assert dependency_key(issue, "#3") == "gh:acme/app#3"
    assert dependency_key(issue, "gh:acme/other#4") == "gh:acme/other#4"


def test_source_for_files_needs_root() -> None:
    with pytest.raises(ValueError, match="checkout"):
        source_for(_config(), Settings())


def test_source_for_github_uses_the_given_app(tmp_path: Path) -> None:
    app = GitHubApp("1", "not-a-key")
    source = source_for(_config("github"), Settings(), tmp_path, app=app)
    assert isinstance(source, GitHubIssuesSource)
    assert source.repo == "acme/tasks"
    assert source.app is app


def test_source_for_github_needs_a_configured_app(tmp_path: Path) -> None:
    settings = Settings(github_app_id=None, github_app_private_key_path=None)
    with pytest.raises(GitHubAppConfigError):
        source_for(_config("github"), settings, tmp_path)


async def test_file_source_through_the_protocol(tmp_path: Path) -> None:
    _write(tmp_path, "M1-01", status="planned")
    _write(tmp_path, "M1-02", deps="M1-01")
    _write(tmp_path, "M1-03", status="done")
    _write(tmp_path, "M1-04", deps="M1-03")
    _write(tmp_path, "M1-05")
    source: TaskSource | None = source_for(_config(), Settings(), tmp_path)
    assert source is not None
    ready = await source.list_ready()
    assert [(r.task.id, r.depth) for r in ready] == [("M1-04", 1), ("M1-05", 0)]
    task = await source.get("task:M1-02")
    assert task is not None
    assert not await source.deps_done(task)
    assert await source.get("task:M9-99") is None


async def test_file_source_write_back_only_logs(tmp_path: Path) -> None:
    _write(tmp_path, "M1-01")
    source = source_for(_config(), Settings(), tmp_path)
    assert source is not None
    pr = PRRef(repo="acme/app", number=1, url="https://github.com/acme/app/pull/1", head_sha="abc")
    key = "task:M1-01"
    await source.comment(key, "hello")
    await source.set_labels(key, ["fact:running"], [])
    await source.write_context(key, "context")
    await source.link_prs(key, [pr])
    before = (tmp_path / "tasks" / "M1" / "01-x.md").read_bytes()
    await source.advance_status(key, TaskStatus.in_progress)
    await source.close(key)
    assert (tmp_path / "tasks" / "M1" / "01-x.md").read_bytes() == before
