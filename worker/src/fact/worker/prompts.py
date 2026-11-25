"""Render a project's prompt template, `.fact/prompts/<name>.md`, for one task."""

import re
from pathlib import Path
from string import Template

from fact.core.types import Task

PROMPTS_DIR = Path(".fact") / "prompts"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class PromptError(ValueError):
    """The prompt is missing, its name is unsafe, or it uses an unknown `$placeholder`."""


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
