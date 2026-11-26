"""
The steps of `FactWorkflow` that act on its PRs: open, comment the cost, wait for CI and fix it, wait for a merge.

`PullRequestSteps` is a mixin of `FactWorkflow` only: it uses the workflow's `input`, `handle`, `check_results`,
`next_attempt`, `note_outcome`, `agent`, `report_task`, `_control`, `_record`, the gate recording helpers of
`HumanSteps`, and the `pr_title`, `pr_summary`, `prs` and `_change_set` its `__init__` sets.
"""

from collections.abc import Sequence
from datetime import timedelta
from typing import TYPE_CHECKING

from temporalio import workflow
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from fact.core.activity_contract import (
        CONTROL_QUEUE,
        MERGE_CHANGE_SET,
        OPEN_CHANGE_SET,
        POST_COST_COMMENT,
        RECORD_GATE_ANSWERED,
        RECORD_GATE_OPENED,
        WAIT_FOR_CI,
        WAIT_FOR_PR_CLOSED,
        CriterionResult,
        MergeChangeSetInput,
        MergeMethod,
        OpenChangeSetInput,
        PostCostCommentInput,
        PRState,
        WaitForCIInput,
        WaitForPRClosedInput,
    )
    from fact.core.enums import RunOutcome, StepOutcome
    from fact.core.gates import NOTE_MAX_CHARS, GateAnswer, GateDecision, OpenGate
    from fact.core.types import PRRef, StepResult
    from fact.sdk.change_set import change_set_input, default_title, text_output
    from fact.sdk.failures import ci_failure_context
    from fact.sdk.policies import HEARTBEAT_TIMEOUT, INFRA_RETRY, LONG_WAIT_RETRY

if TYPE_CHECKING:
    from fact.sdk.workflow import FactWorkflow

OPEN_PR_STAGE = "open_pr"
CI_STAGE = "wait_for_ci"
FIX_CI_STAGE = "fix_ci"
CI_TIMEOUT = timedelta(minutes=60)  # give up on checks still running after this long
CI_ACTIVITY_SLACK = timedelta(minutes=5)  # on top of `CI_TIMEOUT` for the activity's own polling and log downloads
REVIEW_STAGE = "wait_for_review"
REVIEW_TIMEOUT = timedelta(days=30)  # a PR still open after this long fails the run, leaving the workspace


class PullRequestSteps:
    pr_title: str | None  # from the last agent session whose structured answer had a `title`
    pr_summary: str | None  # likewise, `summary`
    prs: list[PRRef]  # from the last push, with the head SHA it pushed
    _change_set: OpenChangeSetInput | None  # the last `open_change_set` input, to push again after a CI fix

    def pr_text(self: FactWorkflow, fallback_summary: str = "") -> tuple[str, str]:
        """PR title and summary: what an agent wrote to its outputs, else a conventional title from the task."""
        task = self.input.task
        return self.pr_title or default_title(task), self.pr_summary or fallback_summary or task.title

    def _remember_pr_text(self, result: StepResult) -> None:
        self.pr_title = text_output(result, "title") or self.pr_title
        self.pr_summary = text_output(result, "summary") or self.pr_summary

    async def open_change_set(
        self: FactWorkflow, title: str, summary: str, criteria: Sequence[CriterionResult] = ()
    ) -> list[PRRef]:
        """Push and open the PR, and post its link on the task; `criteria`, from a review, replace the task's list."""
        spec = change_set_input(self.input, self.handle, title, summary, self.check_results, criteria=list(criteria))
        self._change_set = spec
        prs = await self.push_change_set()
        await self.report_task("prs_opened", prs=prs)
        return prs

    async def push_change_set(self: FactWorkflow) -> list[PRRef]:
        """Push the branch again and update its PR, as the next attempt of stage `open_pr`; sets `prs`."""
        if self._change_set is None:
            msg = "no change set: call `open_change_set` first"
            raise RuntimeError(msg)
        spec = self._change_set.model_copy(update={"attempt": self.next_attempt(OPEN_PR_STAGE)})
        self.prs = await self._control(OPEN_CHANGE_SET, spec, list[PRRef])
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
        result = await workflow.execute_activity(
            WAIT_FOR_CI,
            spec,
            result_type=StepResult,
            task_queue=CONTROL_QUEUE,
            start_to_close_timeout=CI_TIMEOUT + CI_ACTIVITY_SLACK,
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
            retry_policy=INFRA_RETRY,
        )
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

    async def merge_change_set(self: FactWorkflow, prs: Sequence[PRRef], method: MergeMethod = "squash") -> bool:
        """Merge the PRs (each at its pushed head SHA) without a person: False if GitHub refused one, so wait instead."""
        if not prs:
            return False
        spec = MergeChangeSetInput(run_id=self.input.run_id, prs=list(prs), method=method)
        return await self._control(MERGE_CHANGE_SET, spec, bool)

    async def wait_for_pr_closed(self: FactWorkflow, prs: Sequence[PRRef], stage_id: str = REVIEW_STAGE) -> RunOutcome:
        """
        Wait until a person merges or closes every PR: `merged` if all were merged, else `closed`.

        The wait is a gate-like step (stage `stage_id`, kind `gate`): the run shows as waiting, and all of the step's
        time is human wait time. Nobody answers it through `answer_gate`; GitHub does, through the PRs' state. With no
        PR there is nothing to wait for: `pr_opened`. If the wait fails (30 days pass, or GitHub refuses), its step is
        ended `needs_human` and the error raised.
        """
        if not prs:
            return RunOutcome.PR_OPENED
        attempt = self.next_attempt(stage_id)
        urls = [pr.url for pr in prs]
        details = {"summary": f"Review and merge or close: {', '.join(urls)}", "prs": urls}
        gate = OpenGate(
            gate_id=f"{stage_id}-{attempt}",
            kind=stage_id,
            stage_id=stage_id,
            attempt=attempt,
            details=details,
            opened_at=workflow.now(),
        )  # never in `_open_gates`, so `answer_gate` cannot answer it
        step_id = await self._record_gate(RECORD_GATE_OPENED, self._opened_input(gate), str)
        try:
            states = await self._pr_states(prs)
        except ActivityError as exc:
            await self._end_review(gate, step_id, GateDecision.REJECT, f"waiting failed: {exc.cause or exc}")
            raise
        merged = all(state == "merged" for state in states)
        note = ", ".join(f"{url}: {state}" for url, state in zip(urls, states, strict=True))
        await self._end_review(gate, step_id, GateDecision.APPROVE if merged else GateDecision.REJECT, note)
        return RunOutcome.MERGED if merged else RunOutcome.CLOSED

    async def _pr_states(self: FactWorkflow, prs: Sequence[PRRef]) -> list[PRState]:
        return await workflow.execute_activity(
            WAIT_FOR_PR_CLOSED,
            WaitForPRClosedInput(run_id=self.input.run_id, prs=list(prs)),
            result_type=list[PRState],
            task_queue=CONTROL_QUEUE,
            start_to_close_timeout=REVIEW_TIMEOUT,
            schedule_to_close_timeout=REVIEW_TIMEOUT,
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
            retry_policy=LONG_WAIT_RETRY,
        )

    async def _end_review(
        self: FactWorkflow, gate: OpenGate, step_id: str | None, decision: GateDecision, note: str
    ) -> None:
        outcome = StepOutcome.PASSED if decision == GateDecision.APPROVE else StepOutcome.NEEDS_HUMAN
        answer = GateAnswer(gate_id=gate.gate_id, decision=decision, note=note[:NOTE_MAX_CHARS])
        self.note_outcome(gate.stage_id, gate.attempt, outcome)
        answered = self._answered_input(gate, answer, step_id, outcome)
        await self._record_gate(RECORD_GATE_ANSWERED, answered, type(None))
