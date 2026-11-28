"""Pure helpers behind `FactWorkflow.open_change_set`, `pr_text` and the assumptions: no I/O, safe in workflow code."""

from fact.core.activity_contract import CheckResult, CriterionResult, OpenChangeSetInput, PushBranchInput
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
    assumptions: list[str] | None = None,
    spec_ambiguous: bool = False,
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
        assumptions=assumptions or [],
        spec_ambiguous=spec_ambiguous,
        run_url=run.run_url,
    )


def push_input(run: RunInput, change_set: OpenChangeSetInput, attempt: int) -> PushBranchInput:
    """Build the input of `push_branch` for the change set's branch, in the run's first repository."""
    repo = run.repos[0]  # `change_set_input` refused a run without one
    return PushBranchInput(
        run_id=run.run_id,
        handle=change_set.handle,
        repo=repo.full_name,
        branch=change_set.branch,
        default_branch=repo.default_branch,
        attempt=attempt,
    )


def default_title(task: Task) -> str:
    """
    Return a conventional commit subject for the task, such as `feat: <title>`.

    No scope: it names the code area a change touches, which workflow code can't know; the PR body links the task.
    """
    return f"{COMMIT_TYPES.get(task.type, 'feat')}: {task.title}"


def text_output(result: StepResult, key: str) -> str | None:
    """Return a non-blank string output of an agent step, stripped, such as its PR `title` or `summary`."""
    value = result.outputs.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def assumptions_output(result: StepResult) -> list[str]:
    """Return the non-blank `assumptions` an agent step answered, stripped; [] when it gave none."""
    value = result.outputs.get("assumptions")
    if not isinstance(value, list):
        return []
    return [a.strip() for a in value if isinstance(a, str) and a.strip()]


def spec_ambiguous_output(result: StepResult) -> bool:
    """Return whether an agent step answered `spec_ambiguous: true`; anything else is False."""
    return result.outputs.get("spec_ambiguous") is True
