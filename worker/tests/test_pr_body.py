"""The scripted PR description."""

from fact.core.enums import StepOutcome
from fact.core.types import Task
from fact.worker.pr_body import CheckResult, render_pr_body

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
    assert body.startswith("Adds $things to the push path.")
    assert "[M1-11](https://github.com/acme/widget/blob/main/tasks/M1/11-open-change-set.md)" in body
    assert "Push the branch and open the PR" in body
    assert "- [ ] Tests pass" in body
    assert "- [ ] Costs $5 at most" in body
    assert "- **lint**: passed" in body
    assert "- **test**: failed_checks (`pytest` exited with 1)" in body
    assert "more" not in body
    assert "[fact run](http://localhost:8000/runs/r1)" in body


def test_body_without_path_checks_or_run() -> None:
    body = render_pr_body(
        summary="",
        task=_task(path=None, acceptance_criteria=[]),
        repo_url=REPO_URL,
        default_branch="main",
        check_results=[],
        run_url="",
    )
    assert "M1-11: Push the branch and open the PR" in body
    assert "/blob/" not in body
    assert "(none listed)" in body
    assert "(no checks ran)" in body
    assert "(no summary)" in body
