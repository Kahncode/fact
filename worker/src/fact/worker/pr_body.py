"""Render the scripted PR description from `templates/pr_body.md` and the run's data."""

from pathlib import Path
from string import Template

from fact.core.activity_contract import CheckResult, CriterionResult
from fact.core.tasks import parse_key
from fact.core.types import Task

__all__ = ["CheckResult", "CriterionResult", "issue_reference", "render_pr_body", "task_link"]

TEMPLATE_PATH = Path(__file__).parent / "templates" / "pr_body.md"


def _issue(task: Task) -> str | None:
    """`owner/repo#12` for a GitHub issue task, the form GitHub autolinks from any repository; else None."""
    parsed = parse_key(task.key)
    return f"{parsed.repo}#{parsed.id}" if parsed.kind == "gh" else None


def task_link(task: Task, repo_url: str, default_branch: str) -> str:
    """
    `owner/repo#12: title` for an issue, `M1-11: title` for a file task.

    A file task's id links to its file on the default branch.
    """
    label = _issue(task) or task.id
    if task.path:
        label = f"[{task.id}]({repo_url.rstrip('/')}/blob/{default_branch}/{task.path.lstrip('/')})"
    return f"{label}: {task.title}"


def issue_reference(task: Task, pr_count: int = 1) -> str | None:
    """
    Return the line linking the PR to its issue; None for a task that is not a GitHub issue.

    `Closes owner/repo#12` when one PR is the whole change set: GitHub links the two and closes the issue on merge.
    With several PRs, `Part of owner/repo#12` links without closing; fact closes it once every PR has merged.
    """
    issue = _issue(task)
    if issue is None:
        return None
    return f"Closes {issue}" if pr_count == 1 else f"Part of {issue}"


def _checks(results: list[CheckResult]) -> str:
    if not results:
        return "(no checks ran)"
    lines = []
    for r in results:
        first = r.summary.strip().splitlines()[0] if r.summary.strip() else ""
        lines.append(f"- **{r.name}**: {r.outcome}" + (f" ({first})" if first else ""))
    return "\n".join(lines)


def _cell(text: str) -> str:
    """One markdown table cell: pipes escaped, line breaks folded."""
    return " ".join(text.replace("|", "\\|").split()) or "-"


def _criteria(task: Task, reviewed: list[CriterionResult]) -> str:
    """Render the review's verdict per criterion as a table, else the task's criteria as an unchecked list."""
    if not reviewed:
        return "\n".join(f"- [ ] {c}" for c in task.acceptance_criteria) or "(none listed)"
    rows = [f"| {_cell(c.text)} | {'✅ met' if c.met else '❌ not met'} | {_cell(c.evidence)} |" for c in reviewed]
    return "\n".join(["| Criterion | Review | Evidence |", "| --- | --- | --- |", *rows])


def render_pr_body(  # noqa: PLR0913 - options are keyword-only
    *,
    summary: str,
    task: Task,
    repo_url: str,
    default_branch: str,
    check_results: list[CheckResult],
    run_url: str,
    criteria: list[CriterionResult] | None = None,
    pr_count: int = 1,
) -> str:
    """`pr_count`: how many PRs the change set has, which decides between closing and only linking an issue."""
    task_line = task_link(task, repo_url, default_branch)
    if reference := issue_reference(task, pr_count):
        task_line = f"{task_line}\n\n{reference}"
    values = {
        "summary": summary.strip() or "(no summary)",
        "task": task_line,
        "acceptance_criteria": _criteria(task, criteria or []),
        "checks": _checks(check_results),
        "run_link": f"[fact run]({run_url})" if run_url else "fact run: (no link)",
    }
    return Template(TEMPLATE_PATH.read_text(encoding="utf-8")).substitute(values)
