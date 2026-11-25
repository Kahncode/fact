"""Pure helpers behind `FactWorkflow.open_change_set` and `pr_text`: no I/O, safe in workflow code."""

from fact.core.activity_contract import CheckResult, CriterionResult, OpenChangeSetInput
from fact.core.types import RunInput, StepResult, Task, WorkspaceHandle
from temporalio.exceptions import ApplicationError

COMMIT_TYPES = {"feature": "feat", "chore": "chore", "bug": "fix"}


def change_set_input(  # noqa: PLR0913 - the PR's parts, each from a different step
    run: RunInput,
    handle: WorkspaceHandle,
    title: str,
    summary: str,
    check_results: list[CheckResult],
    *,
    criteria: list[CriterionResult] | None = None,
) -> OpenChangeSetInput:
    """Build the input of `open_change_set` for the run's first repository, on the branch the workspace checked out."""
    if not run.repos:
        msg = "the run has no repositories"
        raise ApplicationError(msg, non_retryable=True)
    repo = run.repos[0]
    return OpenChangeSetInput(
        run_id=run.run_id,
        task=run.task,
        handle=handle,
        branch=handle.branches.get(repo.full_name, repo.branch),
        title=title,
        summary=summary,
        check_results=check_results,
        criteria=criteria or [],
        run_url=run.run_url,
    )


def default_title(task: Task) -> str:
    """Return a conventional commit subject for the task, such as `feat(M1-16): <title>`."""
    return f"{COMMIT_TYPES.get(task.type, 'feat')}({task.id}): {task.title}"


def text_output(result: StepResult, key: str) -> str | None:
    """Return a non-blank string output of an agent step, stripped, such as its PR `title` or `summary`."""
    value = result.outputs.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None
