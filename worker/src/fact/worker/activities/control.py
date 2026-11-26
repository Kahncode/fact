"""
Control activities: bookkeeping a workflow needs around its steps.

- `mark_task_done`: delete the task's file on the run's branch and commit that, so the PR (squash-merged) lands the
  work and the task's completion as one commit.
- `record_run_started` and `record_run_finished`: move the run row through the runs API; the start also records the
  worker version the run is pinned to and its home SHA.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fact.core.activity_contract import (
    MARK_TASK_DONE,
    RECORD_RUN_FINISHED,
    RECORD_RUN_STARTED,
    MarkTaskDoneInput,
    RunFinishedInput,
    RunStartedInput,
)
from fact.core.enums import RunStatus
from fact.core.logging import bind_run, get_logger
from fact.core.project_config import ConfigError, load_project_config
from fact.core.settings import Settings, get_settings
from fact.core.tasks import TaskSource, source_for
from fact.worker.git_push import run_git
from fact.worker.loader import home_sha_of
from temporalio import activity
from temporalio.exceptions import ApplicationError

log = get_logger(__name__)

API_PREFIX = "/api/v1"
SERVER_ERROR = 500


def _non_retryable(message: str) -> ApplicationError:
    return ApplicationError(message, non_retryable=True)


def _home_worktree(spec: MarkTaskDoneInput) -> Path:
    repos = spec.handle.repos
    name = spec.home_repo or next(iter(repos), None)
    if name is None or name not in repos:
        msg = f"home repository {name!r} is not in the workspace"
        raise _non_retryable(msg)
    return Path(repos[name])


async def _git_code(args: list[str], worktree: Path) -> tuple[int, list[str]]:
    """
    Run git in the worktree as a person would: their config and the repository's hooks apply.

    Signing is off because nobody is there to unlock a key; pathspecs are literal.
    """
    return await run_git(["--literal-pathspecs", "-c", "commit.gpgSign=false", *args], worktree)


async def _git(args: list[str], worktree: Path) -> None:
    code, lines = await _git_code(args, worktree)
    if code != 0:
        msg = f"git {' '.join(args)} failed ({code}): {' '.join(lines)}"
        raise RuntimeError(msg)


def _task_source(worktree: Path, settings: Settings) -> TaskSource | None:
    try:
        config = load_project_config(worktree)
    except ConfigError as exc:
        raise _non_retryable(str(exc)) from exc
    return source_for(config, settings, worktree)


class ControlActivities:
    def __init__(self, api_base_url: str, http_client: httpx.AsyncClient, settings: Settings | None = None) -> None:
        self._base = api_base_url.rstrip("/") + API_PREFIX
        self._http = http_client
        self._settings = settings

    @activity.defn(name=MARK_TASK_DONE)
    async def mark_task_done(self, spec: MarkTaskDoneInput) -> bool:
        """Commit `chore(<id>): mark done`, deleting the task file, on the run's branch; False when nothing to do."""
        with bind_run(spec.run_id):
            worktree = _home_worktree(spec)
            source = _task_source(worktree, self._settings or get_settings())
            if source is None:
                log.info("mark_task_done_skipped", reason="the task source cannot be read")
                return False
            rel = spec.task.path  # where the task was on the default branch when the run started
            if rel is None:
                msg = f"task {spec.task.key} has no file path"
                raise _non_retryable(msg)
            if (worktree / rel).exists():
                try:
                    source.mark_done(worktree, spec.task.key)
                except KeyError as exc:  # the file at `rel` no longer holds this task
                    raise _non_retryable(str(exc)) from exc
            elif (await _git_code(["cat-file", "-e", f"HEAD:{rel}"], worktree))[0] != 0:
                log.info("mark_task_done_skipped", reason="already deleted on the branch")  # by us, or the agent
                return False
            # Deleted in the worktree, still in HEAD: commit the deletion (also a retry after a failed commit).
            await _git(["rm", "-q", "--cached", "--ignore-unmatch", "--", rel], worktree)
            message = f"chore({spec.task.id}): mark done"
            code, lines = await _git_code(["commit", "-q", "-m", message, "--", rel], worktree)
            if code != 0:  # a hook refused it: retrying would only refuse it again
                msg = f"git commit failed ({code}): {' '.join(lines)}"
                raise _non_retryable(msg)
            log.info("task_marked_done", task_key=spec.task.key, path=rel)
            return True

    @activity.defn(name=RECORD_RUN_STARTED)
    async def record_run_started(self, spec: RunStartedInput) -> None:
        body: dict[str, Any] = {"status": RunStatus.RUNNING, "started_at": _now()}
        if spec.worker_version:
            body |= {"worker_version": spec.worker_version, "home_sha": home_sha_of(spec.worker_version)}
        await self._patch_run(spec.run_id, body)

    @activity.defn(name=RECORD_RUN_FINISHED)
    async def record_run_finished(self, spec: RunFinishedInput) -> None:
        body = {"status": RunStatus.FINISHED, "outcome": spec.outcome, "ended_at": _now()}
        await self._patch_run(spec.run_id, body)

    async def _patch_run(self, run_id: str, body: dict[str, Any]) -> None:
        """PATCH the run; a 4xx will not get better on retry, anything else is left to the activity's retry policy."""
        try:
            response = await self._http.patch(f"{self._base}/runs/{run_id}", json=body)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < SERVER_ERROR:
                msg = f"runs API refused the update of run {run_id}: {exc.response.status_code} {exc.response.text}"
                raise _non_retryable(msg) from exc
            raise
        with bind_run(run_id):
            log.info("run_recorded", **{k: str(v) for k, v in body.items()})


def _now() -> str:
    return datetime.now(UTC).isoformat()
