"""Pure helpers behind opening the change set."""

import pytest
from fact.core.types import Task
from fact.sdk.change_set import default_title


@pytest.mark.parametrize(("task_type", "expected"), [("feature", "feat: Add a widget"), ("bug", "fix: Add a widget")])
def test_default_title_has_no_scope(task_type: str, expected: str) -> None:
    task = Task(key="gh:acme/widget#9", id="9", title="Add a widget", type=task_type)
    assert default_title(task) == expected
