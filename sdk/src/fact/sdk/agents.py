"""
Agent sessions as a mixin of `FactWorkflow`: run a session, resume it when that may help, wait out usage limits.

When to resume is decided in `fact.sdk.sessions`. A session the usage limit refused (`rate_limited`) sleeps on a
durable timer until its `resume_at`, however far away (a full weekly window means days), with the run marked
`waiting` and the `status` query saying `usage limit until <time>`; then the same session is resumed.

`AgentSteps` uses the workflow's `input`, `handle`, `next_attempt`, `with_steering`, `note_*`, `ask_person`, `_record`,
`_activity`, `_remember_pr_text` and `_remember_assumptions`.
"""

from datetime import UTC, timedelta
from typing import TYPE_CHECKING

from temporalio import workflow
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from fact.core.activity_contract import (
        AGENT_STEP,
        RECORD_RUN_WAITING,
        AgentStepInput,
        RunWaitingInput,
    )
    from fact.core.enums import StepOutcome
    from fact.core.gates import GateDecision
    from fact.core.types import StepResult
    from fact.sdk.asking import question_of, resume_context, stopped_by
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
    from fact.sdk.step_defaults import resolve, timeout_s

if TYPE_CHECKING:
    from fact.sdk.workflow import FactWorkflow


def _limit_exhausted(result: StepResult) -> StepResult:
    summary = f"the usage limit stopped the session {MAX_LIMIT_PAUSES + 1} times: {result.summary}"
    return result.model_copy(update={"outcome": StepOutcome.NEEDS_HUMAN, "summary": summary})


class AgentSteps:
    async def agent(
        self: FactWorkflow, stage_id: str, prompt: str, context: str = "", *, pr_text: bool = True, ask: bool = True
    ) -> StepResult:
        """
        Run an agent session on `.fact/prompts/<prompt>.md` and return its last step's result: branch on its outcome.

        The session is resumed once (a new attempt of the stage) if the worker stopped during it, or if the agent
        answered `blocked` with `retryable: true`; see `fact.sdk.sessions`. A session the usage limit refused waits
        for the reset and is resumed, up to `MAX_LIMIT_PAUSES` times, then ends `needs_human`. With `ask`, a session
        answering `blocked` with a `question` asks a person on the task (`fact.sdk.asking`) and is resumed with the
        answer, as many times as it asks; one the person stops stays `needs_human`. Nothing else is retried. With
        `pr_text`, a passed session's `title` and `summary` replace earlier ones for the PR (off for a review: it
        changes nothing); its `assumptions` and `spec_ambiguous` are always kept. Instructions queued by the `steer`
        signal go at the end of the next session's context, then are cleared.
        """
        result = await self._resumed(stage_id, prompt, context, None)
        while ask and (question := question_of(result)) is not None:
            answered = await self.ask_person(stage_id, question)
            if answered.answer.decision != GateDecision.APPROVE:
                result = stopped_by(result, question, answered)
                break
            reply = resume_context(question, answered)
            session = result.session_id
            again = reply if session else f"{context}\n\n{reply}"  # no session to resume: start it over, answered
            result = await self._resumed(stage_id, prompt, again, session)
        if result.outcome == StepOutcome.PASSED:
            self._remember_assumptions(result)
            if pr_text:
                self._remember_pr_text(result)
        return result

    async def _resumed(self: FactWorkflow, stage_id: str, prompt: str, context: str, resume: str | None) -> StepResult:
        """Run (or resume) a session, resuming it as `fact.sdk.sessions` says; return its last step's result."""
        first_context = context
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
                    return _limit_exhausted(result)
                pauses += 1
                resume = await self.wait_out_limit(stage_id, result)
                context = LIMIT_PAUSED if resume else first_context  # no session to resume: start it over
                continue
            reason = retry_reason(result)
            if reason is None or resumes == MAX_RESUMES:
                return result
            resumes += 1
            workflow.logger.info("%s: agent reported a temporary problem; resuming after %s", stage_id, RESUME_DELAY)
            await workflow.sleep(RESUME_DELAY)
            resume, context = result.session_id, f"you reported a temporary problem: {reason}"

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
        limits = resolve(run.step_defaults).agent
        spec = AgentStepInput(
            run_id=run.run_id,
            stage_id=stage_id,
            attempt=self.next_attempt(stage_id),
            handle=self.handle,
            task=run.task,
            prompt_name=prompt.removesuffix(".md"),
            context=self.with_steering(context),
            complexity=run.complexity,
            routing=run.routing,
            max_turns=limits.max_turns,
            permission_mode=limits.permission_mode,
            home_repo=run.home_repo,
            resume_session_id=resume,
            handoff_ref=run.handoff_ref,
        )
        duration = timedelta(seconds=timeout_s(limits))
        result = await self._activity(AGENT_STEP, spec, StepResult, max_duration=duration)
        self.note_outcome(stage_id, spec.attempt, result.outcome)
        self.note_agent_step(stage_id, spec.attempt, result)
        return result
