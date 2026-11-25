"""`agent_step`: run a coding agent on a rendered project prompt in the run's workspace, as a reported step."""

import asyncio
import contextlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fact.core.activity_contract import AGENT_STEP, AgentStepInput
from fact.core.enums import StepKind, StepOutcome
from fact.core.settings import Settings, get_settings
from fact.core.types import Route, StepResult
from fact.worker.harnesses import Harness, HarnessError, HarnessRequest, HarnessResult
from fact.worker.prompts import PromptError, output_schema, render_prompt
from fact.worker.reporting import TRANSCRIPT_NAME, StepContext, StepReporter
from fact.worker.routing import choose
from temporalio import activity
from temporalio.exceptions import ApplicationError

HEARTBEAT_S = 30.0
OUTPUTS_MAX_BYTES = 64 * 1024  # outputs travel in the Temporal payload; anything bigger belongs in an artifact
__all__ = ["AgentActivities", "AgentStepInput"]

MAX_TURNS_REASONS = frozenset({"error_max_turns"})  # harness stop reasons meaning the turn limit was hit


class _Progress:
    """
    Counts assistant turns from the harness's message stream, for heartbeats.

    Claude Code can send one `assistant` message per content block, all with the same `message.id`, so a turn is a
    distinct id; a message without one counts on its own.
    """

    def __init__(self) -> None:
        self.turns = 0
        self._last_id: object = None

    def on_message(self, message: dict[str, Any]) -> None:
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
            activity.heartbeat(f"{stage_id}: {progress.turns} turns")


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
            spec.run_id, spec.stage_id, StepKind.AGENT, spec.attempt, harness=route.harness, model=route.model
        )
        async with reporter as step:
            await self._run(spec, route, step)
        if step.result is None:
            msg = "step ended without a result"
            raise RuntimeError(msg)
        return step.result

    async def _run(self, spec: AgentStepInput, route: Route, step: StepContext) -> None:
        harness = self._harness(route.harness)
        prompt, schema = self._prompt(spec)
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
        )
        step.log(f"{spec.stage_id}: {route.harness} / {route.model}, prompt {spec.prompt_name!r}, cwd {root}")
        progress = _Progress()
        beat = asyncio.create_task(_heartbeats(spec.stage_id, progress))
        try:
            result = await harness.run(request, transcript, progress.on_message)
        except HarnessError as exc:
            step.log(f"harness error: {exc}")
            step.finish(StepOutcome.FAILED_ERROR, str(exc))
            return
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
            if transcript.exists():
                await step.attach_transcript(transcript)
        self._finish(step, result)

    @staticmethod
    def _finish(step: StepContext, result: HarnessResult) -> None:
        step.log(f"stop reason {result.stop_reason}, {result.num_turns} turns, exit code {result.exit_code}")
        for denial in result.permission_denials:
            step.log(f"permission denied: {denial.tool_name} {json.dumps(denial.tool_input)[:500]}")
        outcome, summary, outputs = judge(result)
        step.finish(outcome, summary, outputs, result.usage)

    def _harness(self, name: str) -> Harness:
        harness = self._harnesses.get(name)
        if harness is None:
            msg = f"harness {name!r} is not available on this worker; known: {', '.join(sorted(self._harnesses))}"
            raise ApplicationError(msg, non_retryable=True)
        return harness

    @staticmethod
    def _prompt(spec: AgentStepInput) -> tuple[str, dict[str, Any]]:
        """Return the rendered prompt and the output schema, both from the home repository's `.fact/prompts/`."""
        repos = spec.handle.repos
        name = spec.home_repo or next(iter(repos), None)
        if name is None or name not in repos:
            msg = f"home repository {name!r} is not in the workspace"
            raise ApplicationError(msg, non_retryable=True)
        repo = Path(repos[name])
        try:
            prompt = render_prompt(repo, spec.prompt_name, spec.task, spec.context, spec.handle.root, spec.stage_id)
            return prompt, output_schema(repo, spec.prompt_name)
        except PromptError as exc:
            raise _non_retryable(exc) from exc
