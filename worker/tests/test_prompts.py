"""Prompt rendering from `.fact/prompts/` with `string.Template`."""

from pathlib import Path

import pytest
from fact.core.settings import REPO_ROOT
from fact.core.types import Task
from fact.worker.prompts import PromptError, output_schema, render, render_prompt

TASK = Task(key="task:T-1", id="T-1", title="Add a thing", body="Body text", acceptance_criteria=["one", "two"])


def _write(root: Path, name: str, text: str) -> None:
    folder = root / ".fact" / "prompts"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.md").write_text(text, encoding="utf-8")


def test_all_placeholders(tmp_path: Path) -> None:
    _write(tmp_path, "p", "$task_id|$task_title|$task_body|$acceptance_criteria|$context|$workspace|$stage_id|$$5")
    text = render_prompt(tmp_path, "p", TASK, "ctx", "/ws", "implement")
    assert text == "T-1|Add a thing|Body text|- one\n- two|ctx|/ws|implement|$5"


def test_unreadable_prompt(tmp_path: Path) -> None:
    (tmp_path / ".fact" / "prompts" / "p.md").mkdir(parents=True)  # a directory, not a file
    with pytest.raises(PromptError, match="cannot read"):
        render_prompt(tmp_path, "p", TASK, "", "/ws")


def test_no_criteria(tmp_path: Path) -> None:
    _write(tmp_path, "p", "$acceptance_criteria")
    task = TASK.model_copy(update={"acceptance_criteria": []})
    assert render_prompt(tmp_path, "p", task, "", "/ws") == "(none listed)"


def test_values_are_not_rendered_again() -> None:
    assert render("$context", {"context": "$task_id"}) == "$task_id"


def test_unknown_placeholder(tmp_path: Path) -> None:
    _write(tmp_path, "p", "hello $nope")
    with pytest.raises(PromptError, match=r"\$nope"):
        render_prompt(tmp_path, "p", TASK, "", "/ws")


def test_lone_dollar(tmp_path: Path) -> None:
    _write(tmp_path, "p", "cost $ 5")
    with pytest.raises(PromptError, match=r"\$\$"):
        render_prompt(tmp_path, "p", TASK, "", "/ws")


def test_missing_prompt(tmp_path: Path) -> None:
    with pytest.raises(PromptError, match="not found"):
        render_prompt(tmp_path, "absent", TASK, "", "/ws")


@pytest.mark.parametrize("name", ["../x", "a/b", "a\b", "", ".hidden"])
def test_unsafe_name(tmp_path: Path, name: str) -> None:
    with pytest.raises(PromptError, match="prompt name"):
        render_prompt(tmp_path, name, TASK, "", "/ws")


@pytest.mark.parametrize("name", ["implement", "fix_checks", "review", "address_review"])
def test_shipped_prompts_render(name: str) -> None:
    text = render_prompt(REPO_ROOT, name, TASK, "lint failed", "/ws")
    assert "T-1" in text
    assert "Add a thing" in text
    assert "- one" in text
    assert "$" not in text  # no placeholder left unrendered


def test_implement_answer_requires_ambiguity_and_assumptions() -> None:
    schema = output_schema(REPO_ROOT, "implement")
    assert {"status", "summary", "spec_ambiguous", "assumptions"} <= set(schema["required"])
    assert schema["properties"]["assumptions"]["items"] == {"type": "string"}
