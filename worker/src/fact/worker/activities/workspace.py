"""
`prepare_workspace`: create the run's workspace on a runner, reported as a `shell` step in stage `prepare`.

`sync_base`: merge each repository's default branch into the run's branch, reported as a `shell` step.

`teardown_workspace`: remove it once the run's work is pushed; bookkeeping, not reported as a step.
"""

import asyncio
import contextlib

from fact.core.activity_contract import PREPARE_WORKSPACE, SYNC_BASE, TEARDOWN_WORKSPACE, SyncBaseInput
from fact.core.enums import StepKind, StepOutcome
from fact.core.types import StepResult, WorkspaceHandle, WorkspaceSpec
from fact.worker.base_merge import merge_base
from fact.worker.reporting import StepContext, StepReporter
from fact.worker.runners import Runner
from fact.worker.runners.base import RunnerError
from temporalio import activity
from temporalio.exceptions import ApplicationError

PREPARE_STAGE = "prepare"
HEARTBEAT_S = 30.0


async def _heartbeats(stage_id: str) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_S)
        if activity.in_activity():
            activity.heartbeat(f"{stage_id} running")


class WorkspaceActivities:
    def __init__(self, runner: Runner, reporter: StepReporter) -> None:
        self._runner = runner
        self._reporter = reporter

    @activity.defn(name=PREPARE_WORKSPACE)
    async def prepare_workspace(self, spec: WorkspaceSpec) -> WorkspaceHandle:
        attempt = activity.info().attempt if activity.in_activity() else 1
        async with self._reporter.step(spec.run_id, PREPARE_STAGE, StepKind.SHELL, attempt) as step:
            step.log(f"preparing workspace for {spec.task_key}: {', '.join(r.full_name for r in spec.repos)}")
            handle = await self._runner.prepare(spec)
            for repo in spec.repos:
                branch = handle.branches.get(repo.full_name, repo.branch)
                taken = f" ({repo.branch} is checked out by another workspace)" if branch != repo.branch else ""
                step.log(f"{repo.full_name} -> {handle.repos[repo.full_name]} on {branch}{taken}")
            step.finish(StepOutcome.PASSED, outputs={"root": handle.root, "branches": handle.branches})
        return handle

    @activity.defn(name=SYNC_BASE)
    async def sync_base(self, spec: SyncBaseInput) -> StepResult:
        """
        Fetch, then merge `origin/<default_branch>` into each worktree: `passed`, or `failed_checks` on a conflict.

        `outputs.merged` lists the repositories that got a merge commit, `outputs.conflicts` the unmerged paths per
        repository; a conflicting merge stays in progress for an agent to resolve. A failed fetch is retried; the merge
        failing another way (a dirty worktree, a missing branch) is not: it needs a person. It heartbeats, so an attempt
        is only abandoned when its worker is gone or stuck, and Temporal then cancels it at its next heartbeat; no
        in-process lock, which a retry on another worker or after a restart would not see.
        """
        async with self._reporter.step(spec.run_id, spec.stage_id, StepKind.SHELL, spec.attempt) as step:
            beat = asyncio.create_task(_heartbeats(spec.stage_id))
            try:
                await self._sync(spec, step)
            finally:
                beat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await beat
        if step.result is None:
            msg = "step ended without a result"
            raise RuntimeError(msg)
        return step.result

    async def _sync(self, spec: SyncBaseInput, step: StepContext) -> None:
        merged: list[str] = []
        conflicts: dict[str, list[str]] = {}
        for repo in spec.repos:
            worktree = spec.handle.repos.get(repo.full_name)
            if not worktree:
                continue
            await self._runner.fetch(repo.full_name)  # a `RunnerError` here is retried: the network may come back
            try:
                result = await merge_base(
                    self._runner, spec.handle, worktree, repo.default_branch, step.log, verify_only=spec.verify_only
                )
            except RunnerError as exc:
                raise ApplicationError(str(exc), non_retryable=True) from exc
            step.log(f"{repo.full_name}: {result.state} with origin/{repo.default_branch}")
            if result.state == "merged":
                merged.append(repo.full_name)
            elif result.state == "conflicted":
                conflicts[repo.full_name] = list(result.conflicts)
        outputs = {"merged": merged, "conflicts": conflicts}
        if conflicts:
            files = sum(len(paths) for paths in conflicts.values())
            step.finish(StepOutcome.FAILED_CHECKS, f"{files} files conflict with the base branch", outputs)
        else:
            step.finish(StepOutcome.PASSED, "merged the base branch" if merged else "up to date", outputs)

    @activity.defn(name=TEARDOWN_WORKSPACE)
    async def teardown_workspace(self, handle: WorkspaceHandle) -> None:
        await self._runner.teardown(handle)
