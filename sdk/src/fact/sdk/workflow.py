"""
Workflow base class and stage manifest for project workflows in `.fact/workflows/`.

A project workflow subclasses `FactWorkflow`, declares its stages with `@stages([...])` and calls the helpers, which
run the factory's activities by name (see `fact.core.activity_contract`), so this module never imports the worker.
Workflow code is replayed: it must do no I/O and read no clock; the helpers only schedule activities.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict
from datetime import timedelta
from typing import Any, ClassVar

from temporalio import workflow
from temporalio.exceptions import ActivityError, FailureError

with workflow.unsafe.imports_passed_through():
    from fact.core.activity_contract import (
        CONTROL_QUEUE,
        PREPARE_WORKSPACE,
        RECORD_RUN_FINISHED,
        RECORD_RUN_STARTED,
        SANDBOX_QUEUE,
        SHELL_STEP,
        TEARDOWN_WORKSPACE,
        Check,
        CheckResult,
        OpenChangeSetInput,
        PostCostCommentInput,
        RunFinishedInput,
        RunStartedInput,
        RunWaitingInput,
        ShellStepInput,
    )
    from fact.core.enums import RunOutcome, StepOutcome
    from fact.core.types import PRRef, RunInput, RunResult, StepResult, WorkspaceHandle, WorkspaceSpec
    from fact.sdk.agents import AgentSteps
    from fact.sdk.drop import DropSteps
    from fact.sdk.failures import CONTEXT_MAX_BYTES, failure_context
    from fact.sdk.human import HumanSteps
    from fact.sdk.policies import HEARTBEAT_TIMEOUT, INFRA_RETRY, WAIT_CANCELLED
    from fact.sdk.pull_requests import PullRequestSteps
    from fact.sdk.stages import Stage, stages
    from fact.sdk.task_status import TaskStatusSteps

__all__ = ["CONTEXT_MAX_BYTES", "FactWorkflow", "Stage", "failure_context", "stages"]

# the work is pushed (or no longer wanted), so the workspace goes; any other end leaves it on disk, holding its branch
PUSHED_OUTCOMES = frozenset({RunOutcome.PR_OPENED, RunOutcome.MERGED, RunOutcome.CLOSED, RunOutcome.ABANDONED})


class FactWorkflow(AgentSteps, PullRequestSteps, HumanSteps, DropSteps, TaskStatusSteps):
    """Base class for project workflows: bookkeeping and step helpers; agents, PRs, gates, dropping, the task in mixins."""

    stage_manifest: ClassVar[tuple[Stage, ...]] = ()

    def __init__(self) -> None:
        super().__init__()
        self._input: RunInput | None = None
        self._handle: WorkspaceHandle | None = None
        self._attempts: dict[str, int] = {}
        self.check_results: list[CheckResult] = []  # from the last `until_green` round, for the PR description
        self.pr_title: str | None = None  # from the last agent session whose structured answer had a `title`
        self.pr_summary: str | None = None  # likewise, `summary`
        self.prs: list[PRRef] = []  # from the last `open_change_set`, with the head SHA it pushed
        self._change_set: OpenChangeSetInput | None = None  # its input, to push again after a CI fix

    @classmethod
    def manifest(cls) -> list[dict[str, Any]]:
        """Return the stage manifest as plain data."""
        return [asdict(s) for s in cls.stage_manifest]

    @property
    def input(self) -> RunInput:
        if self._input is None:
            msg = "the run has not started: call `execute` from the workflow's run method"
            raise RuntimeError(msg)
        return self._input

    @property
    def handle(self) -> WorkspaceHandle:
        if self._handle is None:
            msg = "no workspace: call `prepare` first"
            raise RuntimeError(msg)
        return self._handle

    async def execute(self, run_input: RunInput, body: Callable[[], Awaitable[RunResult]]) -> RunResult:
        """
        Record the run as started, run `body`, and record how it finished: `failed` if it raised a Temporal failure.

        The start carries the worker deployment version (build id `<factory SHA>+<home SHA>`) the run is pinned to.
        A run that ends in `PUSHED_OUTCOMES` tears its workspace down; any other leaves it for a person. A dropped
        (cancelled) run ends through `drop`.

        Any other exception is a bug in workflow code: it fails the workflow task, which Temporal retries once the
        code is fixed, so the run is not recorded as finished.
        """
        self._input = run_input
        version = workflow.info().get_current_deployment_version()  # from the workflow task: replay-safe
        started = RunStartedInput(run_id=run_input.run_id, worker_version=version.build_id if version else None)
        try:
            await self._record(RECORD_RUN_STARTED, started)
            await self.report_task("run_started")
            result = await body()
        except (FailureError, asyncio.CancelledError) as exc:  # a cancelled activity may raise either
            if self.drop_requested():
                return await self.drop()
            if isinstance(exc, FailureError):
                await self._record(
                    RECORD_RUN_FINISHED, RunFinishedInput(run_id=run_input.run_id, outcome=RunOutcome.FAILED)
                )
            raise
        if self.drop_requested() and result.outcome not in PUSHED_OUTCOMES:
            return await self.drop()  # dropped as it ended: free the branch for the restart
        self._dropping = True  # the work is over (pushed): a late drop must not stop its bookkeeping
        await self._record(RECORD_RUN_FINISHED, RunFinishedInput(run_id=run_input.run_id, outcome=result.outcome))
        await self.report_run_end(result)
        if result.outcome in PUSHED_OUTCOMES:
            await self._teardown()
        return result

    def next_attempt(self, stage_id: str) -> int:
        """Return the next attempt number of `stage_id` (1, 2, ...): a run's steps are unique by stage and attempt."""
        self.stop_if_dropped()
        self._attempts[stage_id] = self._attempts.get(stage_id, 0) + 1
        self.note_stage(stage_id, self._attempts[stage_id])
        return self._attempts[stage_id]

    async def prepare(self) -> WorkspaceHandle:
        """Create the workspace once; it lasts as long as the run, so its branches and PR never change."""
        if self._handle is not None:
            return self._handle
        run = self.input
        spec = WorkspaceSpec(run_id=run.run_id, task_key=run.task.key, repos=run.repos)
        self._handle = await self._control(PREPARE_WORKSPACE, spec, WorkspaceHandle)
        return self._handle

    async def setup(self, stage_id: str = "setup") -> StepResult | None:
        """Run each repository's `.fact/repo.yaml` `setup` commands; return the first step that failed, else None."""
        for repo in self.handle.repos:
            step = await self._shell(stage_id, repo, "setup")
            if step.outcome != StepOutcome.PASSED:
                return step
        return None

    async def until_green(
        self,
        checks: Sequence[Check],
        fix_prompt: str,
        max_attempts: int,
        *,
        stage_id: str = "checks",
        fix_stage: str = "implement",
    ) -> StepOutcome:
        """
        Run every check in every repository; on failure, a fresh `fix_stage` session gets the failures as context.

        `max_attempts` caps the fix sessions, so checks run at most `max_attempts + 1` times. Returns `passed`, or
        `failed_checks` once the fixes are exhausted or a fix session does not pass. Fixes never write the PR text.
        """
        for fixes in range(max_attempts + 1):
            failures = await self._run_checks(checks, stage_id)
            if not failures:
                return StepOutcome.PASSED
            if fixes == max_attempts:
                break
            fixed = await self.agent(fix_stage, fix_prompt, context=failure_context(failures), pr_text=False)
            if fixed.outcome != StepOutcome.PASSED:
                break  # an errored or over-budget session: another round would only spend more
        return StepOutcome.FAILED_CHECKS

    async def _run_checks(self, checks: Sequence[Check], stage_id: str) -> list[tuple[CheckResult, StepResult]]:
        results: list[tuple[CheckResult, StepResult]] = []
        repos = list(self.handle.repos)
        for repo in repos:
            for check in checks:
                step = await self._shell(stage_id, repo, check)
                name = check if len(repos) == 1 else f"{check} ({repo})"
                results.append((CheckResult(name=name, outcome=step.outcome, summary=step.summary), step))
        self.check_results = [r for r, _ in results]
        return [(r, s) for r, s in results if r.outcome != StepOutcome.PASSED]

    async def _shell(self, stage_id: str, repo: str, check: Check) -> StepResult:
        run = self.input
        spec = ShellStepInput(
            run_id=run.run_id,
            stage_id=stage_id,
            attempt=self.next_attempt(stage_id),
            handle=self.handle,
            repo=repo,
            check=check,
            timeout_s=run.limits.shell_timeout_s,
        )
        result = await workflow.execute_activity(
            SHELL_STEP,
            spec,
            result_type=StepResult,
            task_queue=SANDBOX_QUEUE,
            start_to_close_timeout=timedelta(seconds=run.limits.shell_timeout_s + 60),
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
            retry_policy=INFRA_RETRY,
            cancellation_type=WAIT_CANCELLED,
        )
        self.note_outcome(stage_id, spec.attempt, result.outcome)
        return result

    async def _control[T](self, name: str, arg: object, result_type: type[T]) -> T:
        self.stop_if_dropped()
        return await workflow.execute_activity(
            name,
            arg,
            result_type=result_type,
            task_queue=CONTROL_QUEUE,
            start_to_close_timeout=timedelta(seconds=self.input.limits.control_timeout_s),
            retry_policy=INFRA_RETRY,
        )

    async def _teardown(self) -> None:
        """Best effort: a workspace left behind only takes disk space."""
        if self._handle is None:
            return
        try:
            await self._control(TEARDOWN_WORKSPACE, self._handle, type(None))
        except ActivityError as exc:
            workflow.logger.warning("teardown failed for run %s: %s", self._handle.run_id, exc.cause or exc)

    async def _record(
        self, name: str, arg: RunStartedInput | RunFinishedInput | RunWaitingInput | PostCostCommentInput
    ) -> None:
        """Bookkeeping and evidence are best effort: an API or GitHub outage must not fail a run that did its work."""
        try:
            await self._control(name, arg, type(None))
        except ActivityError as exc:
            workflow.logger.warning("%s failed for run %s: %s", name, arg.run_id, exc.cause or exc)
