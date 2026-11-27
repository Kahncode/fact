"""Pure helpers behind opening the change set."""

import pytest
from fact.core.enums import StepOutcome
from fact.core.types import StepResult, Task
from fact.sdk.change_set import assumptions_output, default_title, spec_ambiguous_output


@pytest.mark.parametrize(("task_type", "expected"), [("feature", "feat: Add a widget"), ("bug", "fix: Add a widget")])
def test_default_title_has_no_scope(task_type: str, expected: str) -> None:
    task = Task(key="gh:acme/widget#9", id="9", title="Add a widget", type=task_type)
    assert default_title(task) == expected


@pytest.mark.parametrize(
    ("outputs", "assumptions", "ambiguous"),
    [
        ({"spec_ambiguous": True, "assumptions": [" Retries default to 3. ", "", 7]}, ["Retries default to 3."], True),
        ({"spec_ambiguous": False, "assumptions": []}, [], False),
        ({"status": "done"}, [], False),  # a fix session that made none
        ({"spec_ambiguous": "yes", "assumptions": "one"}, [], False),  # not the schema's types
    ],
)
def test_assumptions_parse_from_the_answer(
    outputs: dict, assumptions: list[str], ambiguous: bool  # noqa: FBT001 - a parametrize value
) -> None:
    result = StepResult(outcome=StepOutcome.PASSED, summary="done", outputs=outputs)
    assert assumptions_output(result) == assumptions
    assert spec_ambiguous_output(result) is ambiguous
