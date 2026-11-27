"""The scripted PR description."""

import pytest
from fact.core.classification import ChangedPath, Classification, ClassifySignal, SecurityFinding
from fact.core.enums import StepOutcome
from fact.core.types import Task
from fact.worker.pr_body import CheckResult, CriterionResult, render_pr_body

REPO_URL = "https://github.com/acme/widget"


def _task(**extra: object) -> Task:
    values = {
        "key": "task:M1-11",
        "id": "M1-11",
        "title": "Push the branch and open the PR",
        "acceptance_criteria": ["Tests pass", "Costs $5 at most"],
        "path": "tasks/M1/11-open-change-set.md",
        **extra,
    }
    return Task.model_validate(values)


def test_body_has_every_section() -> None:
    checks = [
        CheckResult(name="lint", outcome=StepOutcome.PASSED),
        CheckResult(name="test", outcome=StepOutcome.FAILED_CHECKS, summary="`pytest` exited with 1\nmore"),
    ]
    body = render_pr_body(
        summary="Adds $things to the push path.",
        task=_task(),
        repo_url=REPO_URL,
        default_branch="main",
        check_results=checks,
        run_url="http://localhost:8000/runs/r1",
    )
    assert body == (
        "Adds $things to the push path.\n\n## Task\n\n"
        "[M1-11](https://github.com/acme/widget/blob/main/tasks/M1/11-open-change-set.md): Push the branch and open the PR\n\n"
        "### Acceptance criteria\n\n- [ ] Tests pass\n- [ ] Costs $5 at most\n\n"
        "## Checks\n\n- **lint**: passed\n- **test**: failed_checks (`pytest` exited with 1)\n\n"
        "---\n\n[fact run](http://localhost:8000/runs/r1)\n"
    )


def test_body_without_path_checks_or_run() -> None:
    body = render_pr_body(
        summary="",
        task=_task(path=None, acceptance_criteria=[]),
        repo_url=REPO_URL,
        default_branch="main",
        check_results=[],
        run_url="",
    )
    assert "/blob/" not in body
    assert "(none listed)" in body
    assert "(no checks ran)" in body
    assert "(no summary)" in body


def test_reviewed_criteria_are_a_table() -> None:
    criteria = [
        CriterionResult(text="Tests pass", met=True, evidence="312 passed"),
        CriterionResult(text="a | b", met=False, evidence="line one\nline two"),
        CriterionResult(text="PR shows the table", met=True),
    ]
    body = render_pr_body(
        summary="s",
        task=_task(),
        repo_url=REPO_URL,
        default_branch="main",
        check_results=[],
        run_url="",
        criteria=criteria,
    )
    assert (
        "| Criterion | Review | Evidence |\n| --- | --- | --- |\n"
        "| Tests pass | ✅ met | 312 passed |\n"
        "| a \\| b | ❌ not met | line one line two |\n"
        "| PR shows the table | ✅ met | - |\n"
    ) in body
    assert "- [ ]" not in body


@pytest.mark.parametrize(("pr_count", "reference"), [(1, "Closes acme/tracker#12"), (2, "Part of acme/tracker#12")])
def test_issue_task_links_its_issue_fully_qualified(pr_count: int, reference: str) -> None:
    """The issue lives in acme/tracker, the PR in acme/widget: only `owner/repo#n` reaches it."""
    task = _task(key="gh:acme/tracker#12", id="12", path=None)
    body = render_pr_body(
        summary="s",
        task=task,
        repo_url=REPO_URL,
        default_branch="main",
        check_results=[],
        run_url="",
        pr_count=pr_count,
    )
    assert f"## Task\n\nacme/tracker#12: Push the branch and open the PR\n\n{reference}\n\n### Acceptance" in body
    assert ("Closes" in body) == (pr_count == 1)


def _body(**extra: object) -> str:
    return render_pr_body(
        summary="s", task=_task(), repo_url=REPO_URL, default_branch="main", check_results=[], run_url="", **extra
    )


def test_assumptions_are_listed_after_the_summary() -> None:
    body = _body(assumptions=["Retries default to 3.", "Names\nfollow the API."], spec_ambiguous=True)
    assert body.startswith(
        "s\n\n## Assumptions\n\nThe implementation found the spec ambiguous.\n\n"
        "- Retries default to 3.\n- Names follow the API.\n\n## Task\n"
    )


def test_without_assumptions_there_is_no_section() -> None:
    body = _body(assumptions=[])
    assert "Assumptions" not in body
    assert body.startswith("s\n\n## Task\n")


def test_review_section_lists_reasons_signals_judgments_and_findings() -> None:
    classification = Classification(
        route="human_review",
        reasons=["changes the API surface: `api/openapi.json`", "high risk: touches auth"],
        signals=[ClassifySignal(name="api_change", paths=[ChangedPath(repo="acme/widget", path="api/openapi.json")])],
        risk="high",
        risk_reason="touches auth",
        door="one_way",
        door_reason="drops a column",
        security_findings=[SecurityFinding(file="auth.py", line=4, message="token logged")],
    )
    body = _body(assumptions=["Retries default to 3."], classification=classification)
    assert (
        "- Retries default to 3.\n\n## Review\n\n**Needs review** (`fact:needs-review`):\n\n"
        "- changes the API surface: `api/openapi.json`\n- high risk: touches auth\n\n"
        "### Signals\n\n- `api_change`: `api/openapi.json`\n\n"
        "### Risk and reversibility\n\n- **Risk**: high: touches auth\n- **Door**: one-way: drops a column\n\n"
        "### Security findings\n\n- `auth.py:4` token logged\n\n## Task\n"
    ) in body


def test_auto_merge_review_section() -> None:
    body = _body(classification=Classification(route="auto_merge", risk="low", door="two_way"))
    assert "## Review\n\n**Auto-merge** (`fact:auto-merge`)" in body
    assert "fact:needs-review" not in body
    assert "### Signals\n\nNone." in body
