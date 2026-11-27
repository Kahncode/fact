"""`fact.sdk` schedules activities in one place, from their specs."""

import ast
from pathlib import Path

import pytest
from fact.core.activity_contract import (
    AGENT_STEP,
    CONTROL_QUEUE,
    NOTIFY,
    SHELL_STEP,
    UnknownActivityError,
    spec_for,
)
from fact.core.types import WorkspaceHandle
from fact.sdk.activities import task_queue

SDK_DIR = Path(__file__).resolve().parents[1] / "src" / "fact" / "sdk"


def _execute_activity_calls() -> list[tuple[str, str]]:
    """(file, enclosing function) of every `execute_activity` call in `fact.sdk`."""
    found: list[tuple[str, str]] = []
    for path in sorted(SDK_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            calls = [
                n for n in ast.walk(func) if isinstance(n, ast.Attribute) and n.attr.startswith("execute_activity")
            ]
            found += [(path.name, func.name)] * len(calls)
    return found


def test_the_call_helper_is_the_only_execute_activity() -> None:
    assert _execute_activity_calls() == [("activities.py", "call_activity")]


def test_workspace_activities_go_to_the_handles_queue() -> None:
    handle = WorkspaceHandle(run_id="r", root="/ws", task_queue="runner-7")
    assert task_queue(SHELL_STEP, spec_for(SHELL_STEP), handle) == "runner-7"
    assert task_queue(AGENT_STEP, spec_for(AGENT_STEP), handle) == "runner-7"
    assert task_queue(NOTIFY, spec_for(NOTIFY), handle) == CONTROL_QUEUE


def test_an_activity_without_a_spec_cannot_be_called() -> None:
    with pytest.raises(UnknownActivityError, match="no_such_step"):
        spec_for("no_such_step")
