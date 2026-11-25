from pathlib import Path

import pytest
from fact.core.settings import REPO_ROOT
from fact.core.tasks import FileTaskSource, branch_name

TEMPLATE = """---
id: {id}
title: "Title {id}"
type: chore
complexity: trivial
status: {status}  # keep this comment
depends_on: [{deps}]
human_actions: []
---

## Goal

Do it.

## Acceptance criteria

- [ ] first thing
- [x] second thing

## Out of scope

- [ ] not a criterion
"""


def _task(root: Path, task_id: str, status: str = "planned", deps: str = "", name: str = "x") -> Path:
    milestone, num = task_id.split("-")
    folder = root / "tasks" / milestone
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{num}-{name}.md"
    path.write_text(TEMPLATE.format(id=task_id, status=status, deps=deps), encoding="utf-8", newline="\n")
    return path


def test_parses_task(tmp_path: Path) -> None:
    _task(tmp_path, "M1-02", deps="M1-01, M1-03")
    _task(tmp_path, "M1-03")
    (tmp_path / "tasks" / "README.md").write_text("# not a task", encoding="utf-8")
    (tmp_path / "tasks" / "M1" / "notes.md").write_text("no front matter", encoding="utf-8")
    src = FileTaskSource(tmp_path)
    task = src.get("task:M1-02")
    assert task is not None
    assert (task.id, task.title, task.type, task.complexity) == ("M1-02", "Title M1-02", "chore", "trivial")
    assert task.depends_on == ["M1-01", "M1-03"]
    assert task.acceptance_criteria == ["first thing", "second thing"]
    assert task.path == "tasks/M1/02-x.md"
    assert task.body.startswith("## Goal")
    assert [t.id for t in src.all()] == ["M1-02", "M1-03"]
    assert src.get("task:M9-99") is None
    assert src.validate() == []


def test_skips_moved_tasks(tmp_path: Path) -> None:
    path = _task(tmp_path, "M1-02")
    path.write_text(path.read_text(encoding="utf-8").replace("human_actions", "moved_to: gh:a/b#1\nhuman_actions"))
    assert FileTaskSource(tmp_path).all() == []


def test_ready_and_blocked(tmp_path: Path) -> None:
    _task(tmp_path, "M1-01", status="done")
    _task(tmp_path, "M1-02", status="ready", deps="M1-01")
    _task(tmp_path, "M1-03", status="ready", deps="M1-02")
    _task(tmp_path, "M1-04", status="planned", deps="M1-01")
    src = FileTaskSource(tmp_path)
    assert [t.id for t in src.ready()] == ["M1-02"]
    blocked = src.get("task:M1-03")
    assert blocked is not None
    assert not src.deps_done(blocked)
    assert src.status("task:M1-04") == "planned"
    assert src.validate() == []


def test_deleted_dependency_counts_as_done(tmp_path: Path) -> None:
    _task(tmp_path, "M1-02", status="ready", deps="M0-05, M1-01")
    _task(tmp_path, "M1-09")
    src = FileTaskSource(tmp_path)
    assert [t.id for t in src.ready()] == ["M1-02"]
    assert src.validate() == []


def test_missing_dependency(tmp_path: Path) -> None:
    _task(tmp_path, "M1-02", status="ready", deps="M1-99")
    src = FileTaskSource(tmp_path)
    errors = src.validate()
    assert len(errors) == 1
    assert "M1-02" in errors[0]
    assert "M1-99" in errors[0]
    assert src.ready() == []


def test_cycle_names_both_ids(tmp_path: Path) -> None:
    _task(tmp_path, "M1-02", deps="M1-03")
    _task(tmp_path, "M1-03", deps="M1-02")
    errors = FileTaskSource(tmp_path).validate()
    assert len(errors) == 1
    assert "M1-02" in errors[0]
    assert "M1-03" in errors[0]
    assert "cycle" in errors[0]


def test_validate_reports_bad_files(tmp_path: Path) -> None:
    _task(tmp_path, "M1-02", status="finished")
    _task(tmp_path, "M1-03", name="a")
    _task(tmp_path, "M1-03", name="b")
    wrong = _task(tmp_path, "M1-05")
    wrong.rename(wrong.with_name("06-wrong.md"))
    (tmp_path / "tasks" / "M1" / "07-broken.md").write_text("---\nid: M1-07\n---\n", encoding="utf-8")
    errors = "\n".join(FileTaskSource(tmp_path).validate())
    assert "invalid status 'finished'" in errors
    assert "duplicate id M1-03" in errors
    assert "does not match its file name" in errors
    assert "07-broken.md" in errors


def test_mark_done_changes_only_status(tmp_path: Path) -> None:
    path = _task(tmp_path, "M1-02", status="ready")
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    before = path.read_bytes()
    worktree = tmp_path / "wt"
    copy = worktree / "tasks" / "M1" / path.name
    copy.parent.mkdir(parents=True)
    copy.write_bytes(before)

    FileTaskSource(tmp_path).mark_done(worktree, "task:M1-02")

    assert path.read_bytes() == before  # the source tree is untouched
    assert copy.read_bytes() == before.replace(b"status: ready", b"status: done", 1)


def test_branch_name() -> None:
    assert branch_name("task:M1-02") == "fact/task-m1-02"


def test_real_tasks_folder_is_valid() -> None:
    assert FileTaskSource(REPO_ROOT).validate() == []


def test_mark_done_ignores_status_lines_in_body(tmp_path: Path) -> None:
    path = _task(tmp_path, "M1-02", status="ready")
    text = path.read_text(encoding="utf-8").replace("Do it.", "```\nstatus: example\n```")
    path.write_text(text, encoding="utf-8", newline="\n")
    FileTaskSource(tmp_path).mark_done(tmp_path, "task:M1-02")
    assert "status: done" in path.read_text(encoding="utf-8")
    assert "status: example" in path.read_text(encoding="utf-8")


def test_mark_done_errors(tmp_path: Path) -> None:
    path = _task(tmp_path, "M1-02")
    src = FileTaskSource(tmp_path)
    with pytest.raises(KeyError):
        src.mark_done(tmp_path, "task:M9-01")
    path.write_text(path.read_text(encoding="utf-8").replace("status:", "state:"), encoding="utf-8", newline="\n")
    assert "invalid status ''" in "\n".join(src.validate())
    with pytest.raises(ValueError, match="no status line"):
        src.mark_done(tmp_path, "task:M1-02")


def test_bom_and_bad_yaml(tmp_path: Path) -> None:
    path = _task(tmp_path, "M1-02")
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    bad = _task(tmp_path, "M1-03")
    bad.write_text("---\nid: [unclosed\n---\n", encoding="utf-8")
    src = FileTaskSource(tmp_path)
    assert src.get("task:M1-02") is not None
    assert len(src.validate()) == 1
    assert "03-x.md" in src.validate()[0]


def test_typo_below_highest_is_accepted_as_finished(tmp_path: Path) -> None:
    """Pins the heuristic: a deleted file cannot be told from a typo when its number is below the highest."""
    _task(tmp_path, "M1-09", deps="M1-05")
    assert FileTaskSource(tmp_path).validate() == []
