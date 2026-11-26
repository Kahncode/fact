"""Control activities: marking the task done on the run's branch, and recording the run's start and end."""

import subprocess
from pathlib import Path

import pytest
from fact.core.activity_contract import MarkTaskDoneInput, RunFinishedInput, RunStartedInput
from fact.core.enums import RunOutcome
from fact.core.types import Task, WorkspaceHandle
from fact.worker.activities.control import ControlActivities
from httpx import AsyncClient
from temporalio.exceptions import ApplicationError

BASE = "http://test"
REPO = "acme/widget"
TASK_PATH = "tasks/M1/99-widget.md"
TASK_FILE = """---
id: M1-99
title: Add a widget
type: feature
complexity: standard
status: ready
depends_on: []
human_actions: []
---

## Goal

A widget.
"""
PROJECT_FILE = """name: acme
repositories:
  - full_name: acme/widget
    role: app
task_source:
  kind: files
  path: tasks
workflows:
  default: standard
"""


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)  # noqa: S603, S607
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    repo = tmp_path / "widget"
    (repo / ".fact").mkdir(parents=True)
    (repo / ".fact" / "project.yaml").write_text(PROJECT_FILE, encoding="utf-8")
    (repo / "tasks" / "M1").mkdir(parents=True)
    (repo / TASK_PATH).write_bytes(TASK_FILE.encode())
    _git("init", "-q", "-b", "fact/task-m1-99", cwd=repo)
    _git("config", "user.name", "fact", cwd=repo)
    _git("config", "user.email", "fact@example.com", cwd=repo)
    _git("config", "commit.gpgsign", "false", cwd=repo)
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    return repo


def _spec(worktree: Path, key: str = "task:M1-99") -> MarkTaskDoneInput:
    task_id = key.removeprefix("task:")
    path = TASK_PATH if task_id == "M1-99" else None
    task = Task(key=key, id=task_id, title="Add a widget", path=path)
    handle = WorkspaceHandle(run_id="run-1", root=str(worktree.parent), repos={REPO: str(worktree)})
    return MarkTaskDoneInput(run_id="run-1", task=task, handle=handle)


def _hook(worktree: Path, name: str, body: str) -> None:
    hook = worktree / ".git" / "hooks" / name
    hook.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)


async def test_mark_task_done_deletes_the_task_file(worktree: Path, client: AsyncClient) -> None:
    activities = ControlActivities(BASE, client)
    assert await activities.mark_task_done(_spec(worktree)) is True

    assert not (worktree / TASK_PATH).exists()
    assert _git("log", "-1", "--format=%s", cwd=worktree) == "chore(M1-99): mark done"
    assert _git("show", "--name-status", "--format=", "HEAD", cwd=worktree) == f"D\t{TASK_PATH}"
    assert _git("status", "--porcelain", cwd=worktree) == ""


async def test_mark_task_done_skips_an_issue(worktree: Path, client: AsyncClient) -> None:
    head = _git("rev-parse", "HEAD", cwd=worktree)
    task = Task(key="gh:acme/app#12", id="12", title="An issue")
    handle = WorkspaceHandle(run_id="run-1", root=str(worktree.parent), repos={REPO: str(worktree)})
    spec = MarkTaskDoneInput(run_id="run-1", task=task, handle=handle)
    assert await ControlActivities(BASE, client).mark_task_done(spec) is False
    assert _git("rev-parse", "HEAD", cwd=worktree) == head


async def test_mark_task_done_twice_adds_no_commit(worktree: Path, client: AsyncClient) -> None:
    activities = ControlActivities(BASE, client)
    await activities.mark_task_done(_spec(worktree))
    head = _git("rev-parse", "HEAD", cwd=worktree)

    assert await activities.mark_task_done(_spec(worktree)) is False  # such as a retry after the commit
    assert _git("rev-parse", "HEAD", cwd=worktree) == head


async def test_mark_task_done_retry_after_a_failed_commit(worktree: Path, client: AsyncClient) -> None:
    activities = ControlActivities(BASE, client)
    lock = worktree / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")  # another git process: rm and commit fail
    with pytest.raises(RuntimeError, match="failed"):
        await activities.mark_task_done(_spec(worktree))
    lock.unlink()

    assert not (worktree / TASK_PATH).exists()  # deleted, but never committed
    assert await activities.mark_task_done(_spec(worktree)) is True
    assert _git("log", "-1", "--format=%s", cwd=worktree) == "chore(M1-99): mark done"
    assert _git("status", "--porcelain", cwd=worktree) == ""


async def test_mark_task_done_commits_only_the_task_file(worktree: Path, client: AsyncClient) -> None:
    (worktree / "staged.txt").write_text("agent work", encoding="utf-8")
    _git("add", "staged.txt", cwd=worktree)

    await ControlActivities(BASE, client).mark_task_done(_spec(worktree))
    assert _git("show", "--name-only", "--format=", "HEAD", cwd=worktree) == TASK_PATH
    assert _git("status", "--porcelain", cwd=worktree) == "A  staged.txt"


async def test_mark_task_done_when_the_agent_deleted_the_file(worktree: Path, client: AsyncClient) -> None:
    _git("rm", "-q", TASK_PATH, cwd=worktree)
    _git("commit", "-q", "-m", "feat(M1-99): add a widget", cwd=worktree)
    head = _git("rev-parse", "HEAD", cwd=worktree)

    assert await ControlActivities(BASE, client).mark_task_done(_spec(worktree)) is False
    assert _git("rev-parse", "HEAD", cwd=worktree) == head


async def test_mark_task_done_runs_the_repository_hooks(worktree: Path, client: AsyncClient) -> None:
    _hook(worktree, "pre-commit", "touch hook-ran.txt")
    _hook(worktree, "commit-msg", 'grep -q "^chore(" "$1"')

    await ControlActivities(BASE, client).mark_task_done(_spec(worktree))
    assert (worktree / "hook-ran.txt").exists()


async def test_mark_task_done_fails_when_a_hook_fails(worktree: Path, client: AsyncClient) -> None:
    _hook(worktree, "pre-commit", "exit 1")
    head = _git("rev-parse", "HEAD", cwd=worktree)

    with pytest.raises(ApplicationError, match="commit failed") as info:
        await ControlActivities(BASE, client).mark_task_done(_spec(worktree))
    assert info.value.non_retryable  # a hook would refuse it again
    assert _git("rev-parse", "HEAD", cwd=worktree) == head


async def test_mark_task_done_unknown_task_is_not_retried(worktree: Path, client: AsyncClient) -> None:
    with pytest.raises(ApplicationError, match="task:M1-98") as info:
        await ControlActivities(BASE, client).mark_task_done(_spec(worktree, "task:M1-98"))
    assert info.value.non_retryable


async def test_record_run_started_and_finished(client: AsyncClient, run_id: str) -> None:
    activities = ControlActivities(BASE, client)

    await activities.record_run_started(RunStartedInput(run_id=run_id))
    run = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert run["status"] == "running"
    assert run["started_at"] is not None

    await activities.record_run_finished(RunFinishedInput(run_id=run_id, outcome=RunOutcome.NEEDS_HUMAN))
    run = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert (run["status"], run["outcome"]) == ("finished", "needs_human")
    assert run["ended_at"] is not None


async def test_record_run_started_records_the_pinned_version(client: AsyncClient, run_id: str) -> None:
    home = "0123456789abcdef0123456789abcdef01234567"  # pragma: allowlist secret
    version = f"fedcba987654+{home}"

    await ControlActivities(BASE, client).record_run_started(RunStartedInput(run_id=run_id, worker_version=version))

    run = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert (run["worker_version"], run["home_sha"]) == (version, home)


async def test_record_unknown_run_is_not_retried(client: AsyncClient) -> None:
    with pytest.raises(ApplicationError, match="404") as info:
        await ControlActivities(BASE, client).record_run_started(RunStartedInput(run_id="nope"))
    assert info.value.non_retryable
