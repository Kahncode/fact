"""
`mark_task_done`: delete the task's file on the run's branch and commit that, in one commit with the work once merged.

The PR is squash-merged, so it lands the work and the task's completion as one commit.

A `workspace`-zone activity: every git command runs through `Runner.exec`, so with the `docker` pool the commit is made
in the run's container, and the repository's `pre-commit` and `commit-msg` hooks run there as at any commit.
"""

from fact.core.activity_contract import MARK_TASK_DONE, MarkTaskDoneInput
from fact.core.logging import bind_run, get_logger
from fact.core.tasks import parse_key, task_file_id
from fact.worker.runners.base import Runner
from fact.worker.workspace_git import WorkspaceGit
from temporalio import activity
from temporalio.exceptions import ApplicationError

log = get_logger(__name__)


def _non_retryable(message: str) -> ApplicationError:
    return ApplicationError(message, non_retryable=True)


def _home_worktree(spec: MarkTaskDoneInput) -> str:
    repos = spec.handle.repos
    name = spec.home_repo or next(iter(repos), None)
    if name is None or name not in repos:
        msg = f"home repository {name!r} is not in the workspace"
        raise _non_retryable(msg)
    return repos[name]


async def _git_code(git: WorkspaceGit, *args: str) -> tuple[int, list[str]]:
    """
    Run git in the clone as a person would: their config and the repository's hooks apply.

    Signing is off because nobody is there to unlock a key; pathspecs are literal.
    """
    return await git("--literal-pathspecs", "-c", "commit.gpgSign=false", *args)


async def _git(git: WorkspaceGit, *args: str) -> None:
    code, lines = await _git_code(git, *args)
    if code != 0:
        msg = f"git {' '.join(args)} failed ({code}): {' '.join(lines)}"
        raise RuntimeError(msg)


class TaskDoneActivities:
    def __init__(self, runner: Runner) -> None:
        self._runner = runner

    @activity.defn(name=MARK_TASK_DONE)
    async def mark_task_done(self, spec: MarkTaskDoneInput) -> bool:
        """
        Commit `chore(<id>): mark done`, deleting the task file, on the run's branch; False when nothing to do.

        Only a file task has a file (`Task.path`, where it was on the default branch when the run started). The file
        at that path in HEAD must still hold the task; once HEAD no longer has it (deleted by an earlier attempt, or by
        the agent) there is nothing to do.
        """
        with bind_run(spec.run_id):
            if spec.task.path is None and parse_key(spec.task.key).kind != "task":
                log.info("mark_task_done_skipped", reason="an issue is closed when its PR merges, not by a commit")
                return False
            rel = spec.task.path
            if rel is None:
                msg = f"task {spec.task.key} has no file path"
                raise _non_retryable(msg)
            git = WorkspaceGit(self._runner, spec.handle, _home_worktree(spec))
            code, lines = await _git_code(git, "show", f"HEAD:{rel}")
            if code != 0:
                log.info("mark_task_done_skipped", reason="already deleted on the branch")  # by us, or the agent
                return False
            if task_file_id("\n".join(lines)) != spec.task.id:
                msg = f"{rel} on the branch no longer holds task {spec.task.key}"
                raise _non_retryable(msg)
            # also when the file is already gone from the worktree (a retry after a failed commit)
            await _git(git, "rm", "-q", "-f", "--ignore-unmatch", "--", rel)
            message = f"chore({spec.task.id}): mark done"
            code, lines = await _git_code(git, "commit", "-q", "-m", message, "--", rel)
            if code != 0:  # a hook refused it: retrying would only refuse it again
                msg = f"git commit failed ({code}): {' '.join(lines)}"
                raise _non_retryable(msg)
            log.info("task_marked_done", task_key=spec.task.key, path=rel)
            return True
