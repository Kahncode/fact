"""
Agent sessions as a mixin of `FactWorkflow`: run a session, resume it when that may help, wait out usage limits.

When to resume is decided in `fact.sdk.sessions`. A session the usage limit refused (`rate_limited`) sleeps on a
durable timer until its `resume_at`, however far away (a full weekly window means days), with the run marked
`waiting` and the `status` query saying `usage limit until <time>`; then the same session is resumed.

`AgentSteps` uses the workflow's `input`, `handle`, `next_attempt`, `with_steering`, `note_*`, `_record`,
`_remember_pr_text` and `_remember_assumptions`.
"""

from datetime import UTC, timedelta
from typing import TYPE_CHECKING

from temporalio import workflow
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from fact.core.activity_contract import (
        AGENT_STEP,
        RECORD_RUN_WAITING,
        SANDBOX_QUEUE,
        AgentStepInput,
        RunWaitingInput,
    )
    from fact.core.enums import StepOutcome
    from fact.core.types import StepResult
    from fact.sdk.policies import HEARTBEAT_TIMEOUT, NO_RETRY, WAIT_CANCELLED
    from fact.sdk.sessions import (
        INTERRUPTED,
        LIMIT_PAUSED,
        MAX_LIMIT_PAUSES,
        MAX_RESUMES,
        RESUME_DELAY,
        interrupted_session,
        limit_resume_at,
        limit_session,
        retry_reason,
    )

if TYPE_CHECKING:
    from fact.sdk.workflow import FactWorkflow


def _limit_exhausted(result: StepResult) -> StepResult:
    summary = f"the usage limit stopped the session {MAX_LIMIT_PAUSES + 1} times: {result.summary}"
    return result.model_copy(update={"outcome": StepOutcome.NEEDS_HUMAN, "summary": summary})


class AgentSteps:
    async def agent(
        self: FactWorkflow, stage_id: str, prompt: str, context: str = "", *, pr_text: bool = True
    ) -> StepResult:
        """
        Run an agent session on `.fact/prompts/<prompt>.md` and return its last step's result: branch on its outcome.

        The session is resumed once (a new attempt of the stage) if the worker stopped during it, or if the agent
        answered `blocked` with `retryable: true`; see `fact.sdk.sessions`. A session the usage limit refused waits
        for the reset and is resumed, up to `MAX_LIMIT_PAUSES` times, then ends `needs_human`. Nothing else is
        retried. With `pr_text`, a passed session's `title` and `summary` replace earlier ones for the PR (off for a
        review: it changes nothing); its `assumptions` and `spec_ambiguous` are always kept. Instructions queued by the `steer` signal go at the end of the next session's
        context, then are cleared.
        """
        first_context, resume = context, None
        resumes = pauses = 0
        while True:
            try:
                result = await self._agent_session(stage_id, prompt, context, resume)
            except ActivityError as exc:
                resume = interrupted_session(exc)
                if resume is None or resumes == MAX_RESUMES:
                    raise
                resumes += 1
                workflow.logger.warning("%s: worker stopped during the session; resuming %s", stage_id, resume)
                context = INTERRUPTED
                continue
            if result.outcome == StepOutcome.RATE_LIMITED:
                if pauses == MAX_LIMIT_PAUSES:
                    result = _limit_exhausted(result)
                    break
                pauses += 1
                resume = await self.wait_out_limit(stage_id, result)
                context = LIMIT_PAUSED if resume else first_context  # no session to resume: start it over
                continue
            reason = retry_reason(result)
            if reason is None or resumes == MAX_RESUMES:
                break
            resumes += 1
            workflow.logger.info("%s: agent reported a temporary problem; resuming after %s", stage_id, RESUME_DELAY)
            await workflow.sleep(RESUME_DELAY)
            resume, context = result.session_id, f"you reported a temporary problem: {reason}"
        if result.outcome == StepOutcome.PASSED:
            self._remember_assumptions(result)
            if pr_text:
                self._remember_pr_text(result)
        return result

    async def wait_out_limit(self: FactWorkflow, stage_id: str, result: StepResult) -> str | None:
        """Sleep until a `rate_limited` step's `resume_at`, the run marked waiting; return the session to resume."""
        resume_at = limit_resume_at(result, workflow.now()).astimezone(UTC)
        reason = f"usage limit until {resume_at:%Y-%m-%d %H:%M} UTC"
        workflow.logger.info("%s: %s", stage_id, reason)
        self.note_waiting(reason)
        run_id = self.input.run_id
        await self._record(RECORD_RUN_WAITING, RunWaitingInput(run_id=run_id, waiting=True, reason=reason))
        try:
            self.raise_if_stopped()  # a stop that cancelled the record above is not delivered again
            await workflow.sleep(max(resume_at - workflow.now(), timedelta(0)))
        finally:
            self.note_waiting(None)
        await self._record(RECORD_RUN_WAITING, RunWaitingInput(run_id=run_id, waiting=False, reason=reason))
        return limit_session(result)

    async def _agent_session(
        self: FactWorkflow, stage_id: str, prompt: str, context: str, resume: str | None
    ) -> StepResult:
        run = self.input
        spec = AgentStepInput(
            run_id=run.run_id,
            stage_id=stage_id,
            attempt=self.next_attempt(stage_id),
            handle=self.handle,
            task=run.task,
            prompt_name=prompt.removesuffix(".md"),
            context=self.with_steering(context),
            complexity=run.complexity,
            home_repo=run.home_repo,
            resume_session_id=resume,
            handoff_ref=run.handoff_ref,
        )
        result = await workflow.execute_activity(
            AGENT_STEP,
            spec,
            result_type=StepResult,
            task_queue=SANDBOX_QUEUE,
            start_to_close_timeout=timedelta(seconds=run.limits.agent_timeout_s),
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
            retry_policy=NO_RETRY,
            cancellation_type=WAIT_CANCELLED,  # a stop tears the workspace down only once the session stopped
        )
        self.note_outcome(stage_id, spec.attempt, result.outcome)
        self.note_agent_step(stage_id, spec.attempt, result)
        return result
