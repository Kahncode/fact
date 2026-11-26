"""
The steps of `FactWorkflow` that act on its PRs: open or update them, comment the cost, wait for CI and fix it.

`PullRequestSteps` is a mixin of `FactWorkflow` only: it uses the workflow's `input`, `handle`, `check_results`,
`next_attempt`, `agent`, `_control` and `_record`, and the `prs` and `_change_set` its `__init__` sets.
"""

from collections.abc import Sequence
from datetime import timedelta
from typing import TYPE_CHECKING

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from fact.core.activity_contract import (
        CONTROL_QUEUE,
        OPEN_CHANGE_SET,
        POST_COST_COMMENT,
        WAIT_FOR_CI,
        CriterionResult,
        OpenChangeSetInput,
        PostCostCommentInput,
        WaitForCIInput,
    )
    from fact.core.enums import StepOutcome
    from fact.core.types import PRRef, StepResult
    from fact.sdk.change_set import change_set_input
    from fact.sdk.failures import ci_failure_context
    from fact.sdk.policies import HEARTBEAT_TIMEOUT, INFRA_RETRY

if TYPE_CHECKING:
    from fact.sdk.workflow import FactWorkflow

OPEN_PR_STAGE = "open_pr"
CI_STAGE = "wait_for_ci"
FIX_CI_STAGE = "fix_ci"
CI_TIMEOUT = timedelta(minutes=60)  # give up on checks still running after this long
CI_ACTIVITY_SLACK = timedelta(minutes=5)  # on top of `CI_TIMEOUT` for the activity's own polling and log downloads


class PullRequestSteps:
    prs: list[PRRef]  # from the last push, with the head SHA it pushed
    _change_set: OpenChangeSetInput | None  # the last `open_change_set` input, to push again after a CI fix

    async def open_change_set(
        self: FactWorkflow, title: str, summary: str, criteria: Sequence[CriterionResult] = ()
    ) -> list[PRRef]:
        """Push and open the PR; `criteria`, from a review, replace the task's plain list in its description."""
        spec = change_set_input(self.input, self.handle, title, summary, self.check_results, criteria=list(criteria))
        self._change_set = spec
        return await self.push_change_set()

    async def push_change_set(self: FactWorkflow) -> list[PRRef]:
        """Push the branch again and update its PR, as the next attempt of stage `open_pr`; sets `prs`."""
        if self._change_set is None:
            msg = "no change set: call `open_change_set` first"
            raise RuntimeError(msg)
        spec = self._change_set.model_copy(update={"attempt": self.next_attempt(OPEN_PR_STAGE)})
        self.prs = await self._control(OPEN_CHANGE_SET, spec, list[PRRef])
        return self.prs

    async def post_cost_comment(self: FactWorkflow, prs: Sequence[PRRef]) -> None:
        """Comment the run's time and cost per step and per model on each PR, or update it; best effort."""
        if prs:
            await self._record(POST_COST_COMMENT, PostCostCommentInput(run_id=self.input.run_id, prs=list(prs)))

    async def wait_for_ci(self: FactWorkflow, pr: PRRef, stage_id: str = CI_STAGE) -> StepResult:
        """Wait for CI on the PR's head commit: `passed` or `failed_checks` (`outputs.timed_out` if it never ended)."""
        spec = WaitForCIInput(
            run_id=self.input.run_id,
            stage_id=stage_id,
            attempt=self.next_attempt(stage_id),
            pr=pr,
            timeout_s=CI_TIMEOUT.total_seconds(),
        )
        return await workflow.execute_activity(
            WAIT_FOR_CI,
            spec,
            result_type=StepResult,
            task_queue=CONTROL_QUEUE,
            start_to_close_timeout=CI_TIMEOUT + CI_ACTIVITY_SLACK,
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
            retry_policy=INFRA_RETRY,
        )

    async def until_ci_green(
        self: FactWorkflow,
        prs: Sequence[PRRef],
        fix_prompt: str,
        max_attempts: int,
        *,
        stage_id: str = CI_STAGE,
        fix_stage: str = FIX_CI_STAGE,
    ) -> StepOutcome:
        """
        Wait for CI on every PR; on failure, a fresh `fix_stage` session gets the failing jobs, then push and wait again.

        `max_attempts` caps the fix sessions. Returns `passed`, or `needs_human` once the fixes are exhausted, a fix
        session does not pass or commits nothing, or CI never finished. `prs` holds the PRs at their latest head SHA.
        """
        current = list(prs)
        for fixes in range(max_attempts + 1):
            results = [(pr, await self.wait_for_ci(pr, stage_id)) for pr in current]
            failed = [(pr, r) for pr, r in results if r.outcome != StepOutcome.PASSED]
            if not failed:
                return StepOutcome.PASSED
            if fixes == max_attempts or any(r.outputs.get("timed_out") for _, r in failed):
                break  # a fix cannot make a stuck CI finish
            fixed = await self.agent(fix_stage, fix_prompt, context=ci_failure_context(failed), pr_text=False)
            if fixed.outcome != StepOutcome.PASSED:
                break
            pushed = await self.push_change_set()
            if [pr.head_sha for pr in pushed] == [pr.head_sha for pr in current]:
                workflow.logger.warning("%s: the fix session committed nothing; CI would fail the same way", fix_stage)
                break
            current = pushed
        return StepOutcome.NEEDS_HUMAN
