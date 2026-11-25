import json
from pathlib import Path

import pytest
from claude_log import format_line
from claude_run import RunResult
from loop_report import blocker_report
from task_files import parse_list


def task_file(tasks_dir: Path, task_id: str, depends_on: str = "[]", actions: str = " []") -> None:
    folder = tasks_dir / "M1"
    folder.mkdir(parents=True, exist_ok=True)
    text = f"---\nid: {task_id}\nstatus: planned\ndepends_on: {depends_on}\nhuman_actions:{actions}\n---\n\n## Goal\n"
    (folder / f"{task_id}.md").write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        ("human_actions: []\n", []),
        ("human_actions: []   # comment\nother: 1\n", []),
        ('human_actions: ["a", b]\n', ["a", "b"]),
        ('human_actions:\n  - "Install the app: kc-fact"\n  - b\nnext: 1\n', ["Install the app: kc-fact", "b"]),
        ("depends_on: []\n", []),
    ],
)
def test_parse_list(block: str, expected: list[str]) -> None:
    assert parse_list(block, "human_actions") == expected


@pytest.mark.parametrize(
    ("text", "blocker"),
    [
        ("All done, committed.", ""),
        ("Some notes.\n\nBLOCKED: the App is not installed", "the App is not installed"),
        ("**BLOCKED:** key missing", "key missing"),
        ("BLOCKED: old\nmore\nBLOCKED: new", "new"),
    ],
)
def test_blocker(text: str, blocker: str) -> None:
    result = RunResult(text=text)
    assert result.blocker == blocker
    assert result.failure_reason().startswith("BLOCKED: " if blocker else "exit 0")


def test_failure_reason_keeps_the_end_on_a_word() -> None:
    reason = RunResult(returncode=1, text="word " * 500 + "the end").failure_reason()
    assert reason.startswith("exit 1: ...word")
    assert reason.endswith("the end")


def test_blocker_report(tmp_path: Path) -> None:
    task_file(tmp_path, "M1-01")
    task_file(tmp_path, "M1-02", "[M1-01]")
    task_file(tmp_path, "M1-03", "[M1-02]", '\n  - "Approve the plan"')
    task_file(tmp_path, "M1-04", "[M1-03]")
    report = blocker_report({"M1-01": "install the App"}, tmp_path)
    assert report.splitlines() == [
        "M1-01 (M1-01.md)",
        "    - agent: install the App",
        "    holds up: M1-02, M1-03, M1-04",
        "M1-03 (M1-03.md)",
        "    - human action: Approve the plan",
        "    holds up: M1-04",
    ]
    assert blocker_report({}, tmp_path).startswith("M1-03")


def test_blocker_report_empty(tmp_path: Path) -> None:
    task_file(tmp_path, "M1-01")
    assert blocker_report({}, tmp_path) == ""


def test_format_line() -> None:
    tool = {"type": "tool_use", "name": "Bash", "input": {"command": "git status", "description": "status"}}
    text = {"type": "text", "text": "Checking."}
    assert format_line(json.dumps({"type": "assistant", "message": {"content": [text, tool]}})) == (
        "claude:\n    Checking.\n> Bash: command=git status, description=status"
    )
    result = {"type": "tool_result", "content": [{"type": "text", "text": "x" * 900}], "is_error": True}
    out = format_line(json.dumps({"type": "user", "message": {"content": [result]}}))
    assert out is not None
    assert out.startswith("< error\n    xxx")
    assert out.endswith("[100 more chars]")
    assert format_line(json.dumps({"type": "system", "subtype": "hook_started"})) is None
    assert format_line("plain stderr line\n") == "plain stderr line"
