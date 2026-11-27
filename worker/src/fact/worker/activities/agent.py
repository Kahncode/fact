"""`agent_step`: run a coding agent on a rendered project prompt in the run's workspace, as a reported step."""

import asyncio
import contextlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fact.core.activity_contract import AGENT_INTERRUPTED, AGENT_STEP, AgentStepInput
from fact.core.claude_usage import LIMIT_FALLBACK
from fact.core.enums import StepKind, StepOutcome
from fact.core.settings import Settings, get_settings
from fact.core.types import Route, StepResult
from fact.worker.harnesses import Harness, HarnessError, HarnessRequest, HarnessResult
from fact.worker.harnesses.claude_code_usage import StreamUsage
from fact.worker.prompts import PromptError, output_schema, render_prompt, render_resume
from fact.worker.reporting import TRANSCRIPT_NAME, StepContext, StepReporter
from fact.worker.routing import choose
from temporalio import activity
from temporalio.exceptions import ApplicationError

HEARTBEAT_S = 30.0
OUTPUTS_MAX_BYTES = 64 * 1024  # outputs travel in the Temporal payload; anything bigger belongs in an artifact
__all__ = ["AgentActivities", "AgentStepInput"]

MAX_TURNS_REASONS = frozenset({"error_max_turns"})  # harness stop reasons meaning the turn limit was hit
USAGE_ACCOUNT = "claude"  # one subscription account until runner pools give each its own


class _Progress:
    """
    Counts assistant turns from the harness's message stream, for heartbeats, and keeps the session id.

    It also keeps the usage the messages reported, recorded if the session is cut short before its final report.

    Claude Code can send one `assistant` message per content block, all with the same `message.id`, so a turn is a
    distinct id; a message without one counts on its own.
    """

    def __init__(self) -> None:
        self.turns = 0
        self.session_id: str | None = None
        self.stream = StreamUsage()
        self._last_id: object = None

    def on_message(self, message: dict[str, Any]) -> None:
        if isinstance(sid := message.get("session_id"), str):
            self.session_id = sid
        self.stream.add(message)
        if message.get("type") != "assistant":
            return
        inner = message.get("message")
        message_id = inner.get("id") if isinstance(inner, dict) else None
        if message_id is None or message_id != self._last_id:
            self.turns += 1
        self._last_id = message_id


async def _heartbeats(stage_id: str, progress: _Progress) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_S)
        if activity.in_activity():
            # The session id lets the workflow resume the session if this worker dies (see AGENT_INTERRUPTED).
            activity.heartbeat(f"{stage_id}: {progress.turns} turns", progress.session_id)


def _denied(result: HarnessResult) -> str:
    """One line naming the tool calls the harness refused, or "" when there were none."""
    if not result.permission_denials:
        return ""
    names = sorted({d.tool_name for d in result.permission_denials})
    return f"{len(result.permission_denials)} tool calls denied ({', '.join(names)})"


def judge(result: HarnessResult) -> tuple[StepOutcome, str, dict[str, Any]]:
    """
    Outcome, summary and outputs of an agent session, from its structured final answer rather than its exit code.

    `passed` needs `status: done`; `blocked` is `needs_human`; no structured answer, a harness error or oversized
    outputs is `failed_error`; the turn limit is `stopped_by_monitor`. Denied tool calls are noted in the summary.
    """
    answer = result.structured_output
    denied = _denied(result)
    if result.stop_reason in MAX_TURNS_REASONS:
        outcome, summary = StepOutcome.STOPPED_BY_MONITOR, result.final_message
    elif result.is_error:
        outcome, summary = StepOutcome.FAILED_ERROR, result.final_message
    elif answer is None:
        outcome, summary = (
            StepOutcome.FAILED_ERROR,
            f"the agent ended without a structured answer: {result.final_message}",
        )
    elif answer.get("status") == "done":
        outcome, summary = StepOutcome.PASSED, str(answer.get("summary") or result.final_message)
    else:
        outcome = StepOutcome.NEEDS_HUMAN
        summary = str(answer.get("blocker") or answer.get("summary") or result.final_message)
    outputs = dict(answer or {})
    if (size := len(json.dumps(outputs).encode("utf-8"))) > OUTPUTS_MAX_BYTES:
        outputs = {}
        if outcome == StepOutcome.PASSED:
            outcome = StepOutcome.FAILED_ERROR
            summary = f"structured answer is {size} bytes, over the {OUTPUTS_MAX_BYTES} byte limit"
    if denied:
        outputs["permission_denials"] = [d.model_dump() for d in result.permission_denials]
        if outcome != StepOutcome.PASSED:
            summary = f"{summary}\n\n{denied}"
    return outcome, summary, outputs


def rate_limited(result: HarnessResult, session_id: str | None, now: datetime) -> tuple[str, dict[str, Any]]:
    """Summary and outputs (`resume_at` ISO, `session_id`) of a session the usage limit refused."""
    resume_at = result.usage_windows.resume_at(1.0)
    if resume_at is None or resume_at <= now:
        resume_at = now + LIMIT_FALLBACK
    summary = f"usage limit until {resume_at.isoformat()}: {result.final_message}"
    return summary, {"resume_at": resume_at.isoformat(), "session_id": result.session_id or session_id}


def _non_retryable(exc: Exception) -> ApplicationError:
    return ApplicationError(str(exc), type=type(exc).__name__, non_retryable=True)


class AgentActivities:
    def __init__(
        self, harnesses: Mapping[str, Harness], reporter: StepReporter, settings: Settings | None = None
    ) -> None:
        self._harnesses = dict(harnesses)
        self._reporter = reporter
        self._settings = settings or get_settings()

    @activity.defn(name=AGENT_STEP)
    async def agent_step(self, spec: AgentStepInput) -> StepResult:
        route = choose(spec.complexity, spec.stage_id, spec.override, self._settings.routing)
        reporter = self._reporter.step(
            spec.run_id,
            spec.stage_id,
            StepKind.AGENT,
            spec.attempt,
            harness=route.harness,
            model=route.model,
            name=spec.prompt_name,
        )
        progress = _Progress()
        try:
            async with reporter as step:
                await self._run(spec, route, step, progress)
        except asyncio.CancelledError:
            details = activity.cancellation_details() if activity.in_activity() else None
            if details is None or not details.worker_shutdown:
                raise
            msg = f"the worker stopped during the {spec.stage_id} session"
            raise ApplicationError(msg, progress.session_id, type=AGENT_INTERRUPTED, non_retryable=True) from None
        if step.result is None:
            msg = "step ended without a result"
            raise RuntimeError(msg)
        return step.result.model_copy(update={"session_id": progress.session_id})

    async def _run(self, spec: AgentStepInput, route: Route, step: StepContext, progress: _Progress) -> None:
        harness = self._harness(route.harness)
        prompt, schema = self._prompt(spec, await self._handoff(spec, step))
        root = Path(spec.handle.root)
        transcript = step.log_path.with_name(TRANSCRIPT_NAME)
        transcript.unlink(missing_ok=True)
        defaults = self._settings.step_defaults.agent
        request = HarnessRequest(
            prompt=prompt,
            cwd=root,
            model=route.model,
            max_turns=defaults.max_turns,
            permission_mode=defaults.permission_mode,
            output_schema=schema,
            resume_session_id=spec.resume_session_id,
        )
        step.log(f"{spec.stage_id}: {route.harness} / {route.model}, prompt {spec.prompt_name!r}, cwd {root}")
        if spec.resume_session_id:
            step.log(f"resuming session {spec.resume_session_id}: {spec.context}")
        beat = asyncio.create_task(_heartbeats(spec.stage_id, progress))
        try:
            result = await harness.run(request, transcript, progress.on_message)
            step.reported_usage = result.usage  # kept if the step is cancelled before `_finish`
        except HarnessError as exc:
            step.log(f"harness error: {exc}")
            step.finish(StepOutcome.FAILED_ERROR, str(exc), usage=progress.stream.usage())
            return
        except BaseException:
            step.reported_usage = progress.stream.usage()  # cancelled or crashed: keep what the messages reported
            raise
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
            if transcript.exists():
                await step.attach_transcript(transcript)
        if result.usage_windows.windows:
            step.log(f"usage windows: {result.usage_windows.summary()}")
            await self._reporter.report_usage(USAGE_ACCOUNT, result.usage_windows)
        self._finish(step, result, progress.session_id)

    @staticmethod
    def _finish(step: StepContext, result: HarnessResult, session_id: str | None) -> None:
        step.log(f"stop reason {result.stop_reason}, {result.num_turns} turns, exit code {result.exit_code}")
        for denial in result.permission_denials:
            step.log(f"permission denied: {denial.tool_name} {json.dumps(denial.tool_input)[:500]}")
        if result.rate_limited:
            summary, outputs = rate_limited(result, session_id, datetime.now(UTC))
            step.log(summary)
            step.finish(StepOutcome.RATE_LIMITED, summary, outputs, result.usage)
            return
        outcome, summary, outputs = judge(result)
        step.finish(outcome, summary, outputs, result.usage)

    def _harness(self, name: str) -> Harness:
        harness = self._harnesses.get(name)
        if harness is None:
            msg = f"harness {name!r} is not available on this worker; known: {', '.join(sorted(self._harnesses))}"
            raise ApplicationError(msg, non_retryable=True)
        return harness

    async def _handoff(self, spec: AgentStepInput, step: StepContext) -> str:
        """Return the handoff note of a stopped run of this task for `$handoff`; "" if there is none or it is unreadable."""
        if not spec.handoff_ref:
            return ""
        note = await self._reporter.download_text(spec.handoff_ref)
        step.log(f"handoff note {spec.handoff_ref}: {'unavailable, left out' if note is None else 'in $handoff'}")
        return note or ""

    @staticmethod
    def _prompt(spec: AgentStepInput, handoff: str = "") -> tuple[str, dict[str, Any]]:
        """Return the rendered prompt and the output schema, both from the home repository's `.fact/prompts/`."""
        repos = spec.handle.repos
        name = spec.home_repo or next(iter(repos), None)
        if name is None or name not in repos:
            msg = f"home repository {name!r} is not in the workspace"
            raise ApplicationError(msg, non_retryable=True)
        repo = Path(repos[name])
        try:
            schema = output_schema(repo, spec.prompt_name)  # a resumed session still owes the original answer
            if spec.resume_session_id:
                prompt = render_resume(spec.context)
            else:
                prompt = render_prompt(
                    repo, spec.prompt_name, spec.task, spec.context, spec.handle.root, spec.stage_id, handoff
                )
        except PromptError as exc:
            raise _non_retryable(exc) from exc
        return prompt, schema
