# ruff: noqa: S603, S607, FBT001, PLR0913, PLR0917
import json
import subprocess
from pathlib import Path

import pytest
from claude_run import RunResult, handle_event
from fact.core.tasks import FileTaskSource
from loop_recovery import LoopState, Recovery, add_resume, decide, reopen_task, restore_task_file, tree_is_clean
from task_files import parse_task, runnable_tasks

TASK = """---
id: M1-02
title: "Do the thing"
type: feature
complexity: standard
status: planned
depends_on: [M1-01]
human_actions: []
---

## Goal

Do it.

## Acceptance criteria

- [ ] it works
"""
STATE = LoopState("M1-02", "tasks/M1/02-thing.md", "aaa")


def _window_event(utilization: float, status: str = "allowed") -> dict:
    window = {"utilization": utilization, "resetsAt": 4102444800, "status": status}
    return {
        "type": "rate_limit_event",
        "rate_limit_info": {"status": "allowed", "unifiedWindows": {"five_hour": window}},
    }


@pytest.mark.parametrize(
    ("state", "head", "clean", "exists", "top", "expected"),
    [
        (None, "aaa", True, False, False, Recovery.NONE),
        (None, "aaa", False, False, False, Recovery.DIRTY),
        (STATE, "aaa", True, True, True, Recovery.NONE),  # interrupted before any change
        (STATE, "aaa", False, True, True, Recovery.RESUME),  # cut off before committing
        (STATE, "aaa", False, True, False, Recovery.DIRTY),  # another task is first now: not ours to resume
        (STATE, "aaa", False, False, True, Recovery.DIRTY),
        (STATE, "bbb", True, False, False, Recovery.NONE),  # committed, no limit hit
        (LoopState("M1-02", "p", "aaa", hit_limit=True), "bbb", True, False, False, Recovery.REOPEN),
        (LoopState("M1-02", "p", "aaa", hit_limit=True), "bbb", False, False, False, Recovery.DIRTY),
        (LoopState("M1-02", "p", "aaa", hit_limit=True), "bbb", True, True, True, Recovery.NONE),  # not finished
    ],
)
def test_decide(state: LoopState | None, head: str, clean: bool, exists: bool, top: bool, expected: Recovery) -> None:
    assert decide(state, head=head, clean=clean, task_file_exists=exists, is_top_task=top) is expected


def test_state_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    assert LoopState.load(path) is None
    state = LoopState("M1-02", "tasks/M1/02-thing.md", "aaa", hit_limit=True)
    state.save(path)
    assert LoopState.load(path) == state
    path.write_text("{not json", encoding="utf-8")
    assert LoopState.load(path) is None
    path.write_text(json.dumps({"other": 1}), encoding="utf-8")
    assert LoopState.load(path) is None
    LoopState.clear(path)
    LoopState.clear(path)
    assert not path.exists()


def _write_task(root: Path, text: str, name: str = "02-thing.md") -> Path:
    path = root / "tasks" / "M1" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def test_add_resume_marks_the_task_and_stays_valid(tmp_path: Path) -> None:
    text = add_resume(add_resume(TASK, "a..b"), "a..c")  # reopening twice replaces, not stacks
    assert text.count("resume:") == 1
    assert 'resume: "a..c"' in text
    assert text.count("## Resume") == 1
    assert "commits `a..c` may be incomplete" in text
    assert text.index("## Resume") < text.index("## Goal")
    assert "\n\n\n" not in text
    path = _write_task(tmp_path, text)
    assert parse_task(path).resume == "a..c"
    _write_task(tmp_path, TASK.replace("M1-02", "M1-09"), "09-later.md")
    assert FileTaskSource(tmp_path).validate() == []


def test_reopened_tasks_run_first(tmp_path: Path) -> None:
    _write_task(tmp_path, TASK.replace("M1-02", "M1-01").replace("[M1-01]", "[]"), "01-first.md")
    _write_task(tmp_path, add_resume(TASK.replace("[M1-01]", "[]"), "abc1234"))
    assert [t.id for t in runnable_tasks(tmp_path / "tasks")] == ["M1-02", "M1-01"]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Make a git repo whose first commit adds the task M1-02."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "loop@example.com")
    _git(tmp_path, "config", "user.name", "loop")
    _git(tmp_path, "config", "core.hooksPath", str(tmp_path / "hooks"))
    _write_task(tmp_path, TASK)
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "add task")
    return tmp_path


def _land(repo: Path, message: str = "feat(M1-02): done, maybe") -> str:
    """Delete the task file and commit, as a finished run does; return the short HEAD."""
    (repo / TASK_PATH).unlink()
    (repo / "work.txt").write_text(message, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "--short", "HEAD")


TASK_PATH = "tasks/M1/02-thing.md"


def test_reopen_task_restores_and_commits(repo: Path) -> None:
    head_before = _git(repo, "rev-parse", "HEAD")
    start = _git(repo, "rev-parse", "--short", "HEAD")
    suspect = _land(repo)

    restored = reopen_task(TASK_PATH, head_before, root=repo)

    assert restored == repo / TASK_PATH
    assert parse_task(restored).resume == f"{start}..{suspect}"
    assert tree_is_clean(repo)
    subject = _git(repo, "log", "-1", "--format=%s")
    assert subject == f"chore(tasks): reopen M1-02 to verify {start}..{suspect} after the usage limit"


def test_second_reopen_keeps_the_first_unverified_commit(repo: Path) -> None:
    start = _git(repo, "rev-parse", "--short", "HEAD")
    head_before = _git(repo, "rev-parse", "HEAD")
    _land(repo)
    reopen_task(TASK_PATH, head_before, root=repo)
    reopened = _git(repo, "rev-parse", "HEAD")
    verify = _land(repo, "fix(M1-02): verified, maybe")

    reopen_task(TASK_PATH, reopened, root=repo)

    assert parse_task(repo / TASK_PATH).resume == f"{start}..{verify}"


def test_reopen_reports_a_failing_hook(repo: Path) -> None:
    head_before = _git(repo, "rev-parse", "HEAD")
    _land(repo)
    hook = repo / "hooks" / "pre-commit"
    hook.parent.mkdir()
    hook.write_text("#!/bin/sh\necho hook says no >&2\nexit 1\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)

    with pytest.raises(subprocess.CalledProcessError) as exc:
        reopen_task(TASK_PATH, head_before, root=repo)

    assert "hook says no" in exc.value.stderr


def test_restore_task_file_deleted_but_not_committed(repo: Path) -> None:
    state = LoopState("M1-02", TASK_PATH, _git(repo, "rev-parse", "HEAD"))
    assert not restore_task_file(state, root=repo)  # still there
    (repo / TASK_PATH).unlink()
    assert restore_task_file(state, root=repo)
    assert (repo / TASK_PATH).read_text(encoding="utf-8") == TASK
    _land(repo)
    assert not restore_task_file(state, root=repo)  # committed: the deletion is the task landing


@pytest.mark.parametrize(
    ("events", "hit"),
    [
        ([], False),
        ([{"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}}], True),
        ([{"type": "result", "is_error": True, "result": "Claude usage limit reached"}], True),
        ([{"type": "result", "is_error": True, "result": "Reached max turns limit"}], False),
        ([{"type": "result", "is_error": False, "result": "done"}], False),
        ([_window_event(0.5)], False),
        ([_window_event(1.0)], True),
        ([_window_event(0.4, status="rejected")], True),
    ],
)
def test_hit_limit(events: list[dict], hit: bool) -> None:
    result = RunResult()
    for event in events:
        handle_event(json.dumps(event), result)
    assert result.hit_limit is hit
