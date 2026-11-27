"""Render the scripted PR description from `templates/pr_body.md` and the run's data."""

from pathlib import Path
from string import Template

from fact.core.activity_contract import CheckResult, CriterionResult
from fact.core.classification import Classification
from fact.core.github import AUTO_MERGE_LABEL, NEEDS_REVIEW_LABEL
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


def _assumptions(assumptions: list[str], *, spec_ambiguous: bool) -> str:
    """Return the `## Assumptions` section, between blank lines; empty when there is nothing to say."""
    if not assumptions and not spec_ambiguous:
        return ""
    lines = ["The implementation found the spec ambiguous."] if spec_ambiguous else []
    if assumptions:
        lines.append("\n".join(f"- {' '.join(a.split())}" for a in assumptions))
    return "\n## Assumptions\n\n" + "\n\n".join(lines) + "\n"


def _where(file: str, line: int | None) -> str:
    if not file:
        return ""
    return f"`{file}:{line}` " if line is not None else f"`{file}` "


def _judgment(name: str, value: str | None, reason: str) -> str:
    if value is None:
        return f"- **{name}**: not judged"
    return f"- **{name}**: {value.replace('_', '-')}" + (f": {' '.join(reason.split())}" if reason.strip() else "")


def _review(classification: Classification | None) -> str:
    """Return the `## Review` section, between blank lines; empty before the change set is classified."""
    if classification is None:
        return ""
    c = classification
    if c.route == "auto_merge":
        verdict = f"**Auto-merge** (`{AUTO_MERGE_LABEL}`): no signal asks for a person's review."
    else:
        why = "\n".join(f"- {' '.join(r.split())}" for r in c.reasons)
        verdict = f"**Needs review** (`{NEEDS_REVIEW_LABEL}`):\n\n{why}"
    signals = [
        f"- `{s.name}`" + (": " + ", ".join(f"`{p.path}`" for p in s.paths) if s.paths else "") for s in c.signals
    ]
    judged = [_judgment("Risk", c.risk, c.risk_reason), _judgment("Door", c.door, c.door_reason)]
    findings = [f"- {_where(f.file, f.line)}{' '.join(f.message.split())}" for f in c.security_findings]
    parts = [
        verdict,
        "### Signals\n\n" + ("\n".join(signals) or "None."),
        "### Risk and reversibility\n\n" + "\n".join(judged),
        "### Security findings\n\n" + ("\n".join(findings) or "None."),
    ]
    return "\n## Review\n\n" + "\n\n".join(parts) + "\n"


def render_pr_body(  # noqa: PLR0913 - options are keyword-only
    *,
    summary: str,
    task: Task,
    repo_url: str,
    default_branch: str,
    check_results: list[CheckResult],
    run_url: str,
    criteria: list[CriterionResult] | None = None,
    assumptions: list[str] | None = None,
    spec_ambiguous: bool = False,
    classification: Classification | None = None,
    pr_count: int = 1,
) -> str:
    """
    Render the PR description.

    `pr_count`: how many PRs the change set has, which decides between closing and only linking an issue.
    `assumptions` and `spec_ambiguous`, from the agent sessions, make an `## Assumptions` section after the summary,
    absent when there are none; the description becomes the squash commit's body, so they stay in git history.
    `classification`, once classify ran, makes a `## Review` section after them: the route and its reasons, the
    signals with their paths, risk and door with their reasons, and the security findings.
    """
    task_line = task_link(task, repo_url, default_branch)
    if reference := issue_reference(task, pr_count):
        task_line = f"{task_line}\n\n{reference}"
    values = {
        "summary": summary.strip() or "(no summary)",
        "assumptions": _assumptions(assumptions or [], spec_ambiguous=spec_ambiguous),
        "review": _review(classification),
        "task": task_line,
        "acceptance_criteria": _criteria(task, criteria or []),
        "checks": _checks(check_results),
        "run_link": f"[fact run]({run_url})" if run_url else "fact run: (no link)",
    }
    return Template(TEMPLATE_PATH.read_text(encoding="utf-8")).substitute(values)
