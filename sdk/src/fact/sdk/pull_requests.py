"""
The steps of `FactWorkflow` that act on its PRs: open and push, comment the cost, wait for CI and fix it, merge.

`PullRequestSteps` is a mixin of `FactWorkflow` only: it uses the workflow's `input`, `handle`, `check_results`,
`next_attempt`, `note_outcome`, `agent`, `report_task`, `_activity`, `_record`, and the `pr_title`, `pr_summary`,
`assumptions`, `spec_ambiguous`, `prs`, `pushed_at` and `_change_set` its `__init__` sets. Waiting for review is in
`fact.sdk.review_wait`.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from fact.core.activity_contract import (
        MERGE_CHANGE_SET,
        OPEN_CHANGE_SET,
        POST_COST_COMMENT,
        PUSH_BRANCH,
        WAIT_FOR_CI,
        CriterionResult,
        MergeChangeSetInput,
        MergeMethod,
        MergeResult,
        OpenChangeSetInput,
        PostCostCommentInput,
        WaitForCIInput,
    )
    from fact.core.enums import StepOutcome
    from fact.core.types import PRRef, StepResult
    from fact.sdk.change_set import (
        assumptions_output,
        change_set_input,
        default_title,
        push_input,
        spec_ambiguous_output,
        text_output,
    )
    from fact.sdk.failures import ci_failure_context

if TYPE_CHECKING:
    from fact.sdk.workflow import FactWorkflow

PUSH_STAGE = "push"
OPEN_PR_STAGE = "open_pr"
CI_STAGE = "wait_for_ci"
FIX_CI_STAGE = "fix_ci"
CI_TIMEOUT = timedelta(minutes=60)  # give up on checks still running after this long; `wait_for_ci`'s spec adds 5 min
# how `until_ci_green` ended: green, a PR conflicting with its base (merge the base), or out of fixes
CIOutcome = Literal["passed", "conflict", "needs_human"]


class PullRequestSteps:
    pr_title: str | None  # from the last agent session whose structured answer had a `title`
    pr_summary: str | None  # likewise, `summary`
    assumptions: list[str]  # every passed agent session's `assumptions`, in order, without repeats
    spec_ambiguous: bool  # an agent session answered `spec_ambiguous: true`
    prs: list[PRRef]  # from the last push, with the head SHA it pushed
    pushed_at: datetime | None  # workflow time of the last push, where a review round starts reading comments
    merged_by_fact: bool  # `merge_change_set` merged the change set, so no person needs telling
    _change_set: OpenChangeSetInput | None  # the last `open_change_set` input, to push again after a CI fix

    def pr_text(self: FactWorkflow, fallback_summary: str = "") -> tuple[str, str]:
        """PR title and summary: what an agent wrote to its outputs, else a conventional title from the task."""
        task = self.input.task
        return self.pr_title or default_title(task), self.pr_summary or fallback_summary or task.title

    def _remember_pr_text(self, result: StepResult) -> None:
        self.pr_title = text_output(result, "title") or self.pr_title
        self.pr_summary = text_output(result, "summary") or self.pr_summary

    def _remember_assumptions(self, result: StepResult) -> None:
        """Add a passed session's `assumptions` (fix sessions may add some too) and its `spec_ambiguous` verdict."""
        self.assumptions += [a for a in assumptions_output(result) if a not in self.assumptions]
        self.spec_ambiguous = self.spec_ambiguous or spec_ambiguous_output(result)

    async def open_change_set(
        self: FactWorkflow, title: str, summary: str, criteria: Sequence[CriterionResult] = ()
    ) -> list[PRRef]:
        """Push, open the PR, and post its link on the task; `criteria`, from a review, replace the task's list."""
        spec = change_set_input(
            self.input,
            self.handle,
            title,
            summary,
            self.check_results,
            criteria=list(criteria),
            assumptions=self.assumptions,
            spec_ambiguous=self.spec_ambiguous,
        )
        self._change_set = spec
        prs = await self.push_change_set()
        await self.report_task("prs_opened", prs=prs)
        return prs

    async def push_change_set(self: FactWorkflow, criteria: Sequence[CriterionResult] | None = None) -> list[PRRef]:
        """
        Push the branch again and update its PR, as the next attempts of `push` and `open_pr`; sets `prs`, `pushed_at`.

        `push_branch` pushes from the workspace; `open_change_set` then reads the branch from GitHub. The description takes the new assumptions, and `criteria` (from a new review) when given.
        """
        if self._change_set is None:
            msg = "no change set: call `open_change_set` first"
            raise RuntimeError(msg)
        if criteria is not None:
            self._change_set = self._change_set.model_copy(update={"criteria": list(criteria)})
        update = {
            "attempt": self.next_attempt(OPEN_PR_STAGE),
            "check_results": list(self.check_results),  # a review round runs the checks again
            "assumptions": list(self.assumptions),  # a CI fix session may have added some
            "spec_ambiguous": self.spec_ambiguous,
        }
        spec = self._change_set.model_copy(update=update)
        pushed = push_input(self.input, spec, self.next_attempt(PUSH_STAGE))
        await self._activity(PUSH_BRANCH, pushed, str)
        self.note_outcome(PUSH_STAGE, pushed.attempt, StepOutcome.PASSED)
        self.prs = await self._activity(OPEN_CHANGE_SET, spec, list[PRRef])
        self.pushed_at = workflow.now()
        self.note_outcome(OPEN_PR_STAGE, spec.attempt, StepOutcome.PASSED)
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
        result = await self._activity(WAIT_FOR_CI, spec, StepResult)
        self.note_outcome(stage_id, spec.attempt, result.outcome)
        return result

    async def until_ci_green(
        self: FactWorkflow,
        prs: Sequence[PRRef],
        fix_prompt: str,
        max_attempts: int,
        *,
        stage_id: str = CI_STAGE,
        fix_stage: str = FIX_CI_STAGE,
    ) -> CIOutcome:
        """
        Wait for CI on every PR; on failure, a fresh `fix_stage` session gets the failing jobs, then push and wait again.

        `max_attempts` caps the fix sessions. Returns `passed`; `conflict` at once when a PR conflicts with its base
        (GitHub runs no CI on it: the workflow merges the base, `sync_with_base`, pushes and calls this again); or
        `needs_human` once the fixes are exhausted, a fix session does not pass or commits nothing, or CI never
        finished. `prs` holds the PRs at their latest head SHA.
        """
        current = list(prs)
        for fixes in range(max_attempts + 1):
            results = [(pr, await self.wait_for_ci(pr, stage_id)) for pr in current]
            failed = [(pr, r) for pr, r in results if r.outcome != StepOutcome.PASSED]
            if not failed:
                return "passed"
            if any(r.outputs.get("conflict") for _, r in failed):
                return "conflict"
            if fixes == max_attempts or any(r.outputs.get("timed_out") for _, r in failed):
                break  # a fix cannot make a stuck CI finish
            pushed = await self._fix_ci(failed, fix_prompt, fix_stage)
            if pushed is None:
                break
            if [pr.head_sha for pr in pushed] == [pr.head_sha for pr in current]:
                workflow.logger.warning("%s: the fix session committed nothing; CI would fail the same way", fix_stage)
                break
            current = pushed
        return "needs_human"

    async def _fix_ci(
        self: FactWorkflow, failed: list[tuple[PRRef, StepResult]], fix_prompt: str, fix_stage: str
    ) -> list[PRRef] | None:
        """Run a CI fix session and push what it committed; None if it did not pass."""
        fixed = await self.agent(fix_stage, fix_prompt, context=ci_failure_context(failed), pr_text=False)
        if fixed.outcome != StepOutcome.PASSED:
            return None
        return await self.push_change_set()

    async def merge_change_set(self: FactWorkflow, prs: Sequence[PRRef], method: MergeMethod = "squash") -> MergeResult:
        """
        Merge the PRs (each at its pushed head SHA) without a person: `merged`, or why GitHub refused one.

        `out_of_date`: the PR conflicts with or is behind its base, which `sync_with_base` fixes; `refused`: anything
        else (branch protection, the head moved on), so wait for a person. Once merged, `merged_by_fact` is set and
        the run's end notifies no one.
        """
        if not prs:
            return "refused"
        spec = MergeChangeSetInput(run_id=self.input.run_id, prs=list(prs), method=method)
        merged: MergeResult = await self._activity(MERGE_CHANGE_SET, spec, str)  # type: ignore[assignment]
        self.merged_by_fact = self.merged_by_fact or merged == "merged"
        return merged
