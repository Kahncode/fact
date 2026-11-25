"""Render a project's prompt template, `.fact/prompts/<name>.md`, for one task."""

import json
import re
from pathlib import Path
from string import Template
from typing import Any

from fact.core.types import Task

PROMPTS_DIR = Path(".fact") / "prompts"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class PromptError(ValueError):
    """The prompt is missing, its name is unsafe, it uses an unknown `$placeholder`, or its schema is invalid."""


# Every agent step ends with this JSON object; `.fact/prompts/<name>.schema.json` may add properties to it.
BASE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["done", "blocked"],
            "description": "done: the work is finished and committed. blocked: you could not finish it.",
        },
        "summary": {"type": "string", "description": "A few lines of markdown: what you did and why."},
        "blocker": {"type": "string", "description": "When blocked: what stopped you and what would unblock it."},
        "retryable": {
            "type": "boolean",
            "description": (
                "When blocked: true only if the cause was temporary and outside your control (network, rate limit, a"
                " flaky service) and a later attempt with nothing changed by a person would likely succeed."
            ),
        },
    },
    "required": ["status", "summary"],
}


def prompt_path(repo_root: Path, name: str) -> Path:
    if not _NAME.fullmatch(name):
        msg = f"prompt name {name!r} must be letters, digits, '-' or '_'"
        raise PromptError(msg)
    return repo_root / PROMPTS_DIR / f"{name}.md"


def variables(task: Task, context: str, workspace: str, stage_id: str = "") -> dict[str, str]:
    criteria = "\n".join(f"- {c}" for c in task.acceptance_criteria)
    return {
        "task_id": task.id,
        "task_title": task.title,
        "task_body": task.body,
        "acceptance_criteria": criteria or "(none listed)",
        "context": context,
        "workspace": workspace,
        "stage_id": stage_id,
    }


def render(template: str, values: dict[str, str]) -> str:
    """`string.Template` substitution; `$$` is a literal `$`, an unknown `$name` raises `PromptError`."""
    try:
        return Template(template).substitute(values)
    except KeyError as exc:
        msg = f"unknown placeholder ${exc.args[0]}; known: {', '.join(sorted(values))}"
        raise PromptError(msg) from exc
    except ValueError as exc:  # a lone `$` not followed by a name
        msg = f"invalid placeholder: {exc}; write $$ for a literal $"
        raise PromptError(msg) from exc


def render_prompt(  # noqa: PLR0913, PLR0917
    repo_root: Path, name: str, task: Task, context: str, workspace: str, stage_id: str = ""
) -> str:
    path = prompt_path(repo_root, name)
    try:
        template = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        msg = f"prompt {name!r} not found at {path}"
        raise PromptError(msg) from exc
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"cannot read prompt {name!r} at {path}: {exc}"
        raise PromptError(msg) from exc
    return render(template, variables(task, context, workspace, stage_id))


def output_schema(repo_root: Path, name: str) -> dict[str, Any]:
    """
    Return the JSON Schema the step's final answer must match: `BASE_OUTPUT_SCHEMA` plus the prompt's own schema.

    `.fact/prompts/<name>.schema.json`, when present, is an object with `properties` and optionally `required`; they
    are added to the base, which keeps its own fields.
    """
    path = prompt_path(repo_root, name).with_suffix(".schema.json")
    try:
        extra = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return BASE_OUTPUT_SCHEMA
    except (OSError, ValueError) as exc:
        msg = f"cannot read output schema {path}: {exc}"
        raise PromptError(msg) from exc
    props = extra.get("properties") if isinstance(extra, dict) else None
    required = extra.get("required", []) if isinstance(extra, dict) else None
    if not isinstance(props, dict) or not isinstance(required, list):
        msg = f"{path} must be an object with a `properties` object and an optional `required` list"
        raise PromptError(msg)
    base_required = BASE_OUTPUT_SCHEMA["required"]
    return {
        **BASE_OUTPUT_SCHEMA,
        "properties": {**props, **BASE_OUTPUT_SCHEMA["properties"]},
        "required": base_required + [r for r in required if r not in base_required],
    }


RESUME_TEMPLATE = """Your previous session on this task stopped before it finished: $context

The workspace is as you left it. Check its state first (`git status` and `git log` in each repository you touched),
then continue the task from where you stopped. End with the structured answer, as before."""


def render_resume(reason: str) -> str:
    """Render the prompt that resumes an agent session, saying why it stopped."""
    return Template(RESUME_TEMPLATE).substitute(context=reason)
