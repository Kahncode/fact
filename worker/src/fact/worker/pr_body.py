"""Render the scripted PR description from `templates/pr_body.md` and the run's data."""

from pathlib import Path
from string import Template

from fact.core.activity_contract import CheckResult
from fact.core.types import Task

__all__ = ["CheckResult", "render_pr_body", "task_link"]

TEMPLATE_PATH = Path(__file__).parent / "templates" / "pr_body.md"


def task_link(task: Task, repo_url: str, default_branch: str) -> str:
    """`M1-11: title`, the id linking to the task file on the default branch when the task is a file."""
    label = task.id
    if task.path:
        label = f"[{task.id}]({repo_url.rstrip('/')}/blob/{default_branch}/{task.path.lstrip('/')})"
    return f"{label}: {task.title}"


def _checks(results: list[CheckResult]) -> str:
    if not results:
        return "(no checks ran)"
    lines = []
    for r in results:
        first = r.summary.strip().splitlines()[0] if r.summary.strip() else ""
        lines.append(f"- **{r.name}**: {r.outcome}" + (f" ({first})" if first else ""))
    return "\n".join(lines)


def render_pr_body(  # noqa: PLR0913 - options are keyword-only
    *,
    summary: str,
    task: Task,
    repo_url: str,
    default_branch: str,
    check_results: list[CheckResult],
    run_url: str,
) -> str:
    criteria = "\n".join(f"- [ ] {c}" for c in task.acceptance_criteria) or "(none listed)"
    values = {
        "summary": summary.strip() or "(no summary)",
        "task": task_link(task, repo_url, default_branch),
        "acceptance_criteria": criteria,
        "checks": _checks(check_results),
        "run_link": f"[fact run]({run_url})" if run_url else "fact run: (no link)",
    }
    return Template(TEMPLATE_PATH.read_text(encoding="utf-8")).substitute(values)
