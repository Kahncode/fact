"""
Control activities: bookkeeping a workflow needs around its steps.

- `mark_task_done`: set the task file's `status: done` on the run's branch and commit it, so the PR (squash-merged)
  lands the work and the task's completion as one commit.
- `record_run_started` and `record_run_finished`: move the run row through the runs API; the start also records the
  worker version the run is pinned to and its home SHA.
"""

import tempfile
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
from fact.core.tasks.files import FileTaskSource
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


async def _git(args: list[str], worktree: Path, hooks_dir: Path) -> list[str]:
    """Run git in the worktree with hooks, fsmonitor and signing off: the control zone runs none of the worktree's code."""
    hardening = ["-c", f"core.hooksPath={hooks_dir}", "-c", "core.fsmonitor=false", "-c", "commit.gpgSign=false"]
    code, lines = await run_git([*hardening, *args], worktree, env={"GIT_CONFIG_NOSYSTEM": "1"})
    if code != 0:
        msg = f"git {' '.join(args)} failed ({code}): {' '.join(lines)}"
        raise RuntimeError(msg)
    return lines


def _task_source(worktree: Path) -> FileTaskSource | None:
    try:
        config = load_project_config(worktree).task_source
    except ConfigError as exc:
        raise _non_retryable(str(exc)) from exc
    return FileTaskSource(worktree, config.path) if config.kind == "files" else None


def _task_path(source: FileTaskSource, key: str) -> str:
    found = source.get(key)
    if found is None or found.path is None:
        msg = f"task {key} is not in {source.folder}/ on the run's branch"
        raise _non_retryable(msg)
    return found.path


class ControlActivities:
    def __init__(self, api_base_url: str, http_client: httpx.AsyncClient) -> None:
        self._base = api_base_url.rstrip("/") + API_PREFIX
        self._http = http_client

    @activity.defn(name=MARK_TASK_DONE)
    async def mark_task_done(self, spec: MarkTaskDoneInput) -> bool:
        """Commit `chore(<id>): mark done` on the run's branch; False when there was nothing to mark."""
        with bind_run(spec.run_id):
            worktree = _home_worktree(spec)
            source = _task_source(worktree)
            if source is None:
                log.info("mark_task_done_skipped", reason="the task source is not files")
                return False
            rel = _task_path(source, spec.task.key)
            with tempfile.TemporaryDirectory(prefix="fact-hooks-") as hooks:
                hooks_dir = Path(hooks)
                if source.status(spec.task.key) != "done":
                    try:
                        source.mark_done(worktree, spec.task.key)
                    except (KeyError, ValueError) as exc:
                        raise _non_retryable(str(exc)) from exc
                elif not await _git(["status", "--porcelain", "--", rel], worktree, hooks_dir):
                    log.info("mark_task_done_skipped", reason="already done")  # done on the branch, or a retry
                    return False
                # Done but not committed: a retry after a failed commit. Commit only the task file.
                await _git(["add", "--", rel], worktree, hooks_dir)
                message = f"chore({spec.task.id}): mark done"
                await _git(["commit", "--no-verify", "-m", message, "--", rel], worktree, hooks_dir)
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
