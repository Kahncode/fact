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
from fact.worker.prompts import PromptError, render_prompt
from fact.worker.reporting import TRANSCRIPT_NAME, StepContext, StepReporter
from fact.worker.routing import choose
from temporalio import activity
from temporalio.exceptions import ApplicationError

HEARTBEAT_S = 30.0
OUT_DIR = ".fact-out"  # at the workspace root, outside every repository, so agents never commit it
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


def _outcome(result: HarnessResult) -> StepOutcome:
    if result.stop_reason in MAX_TURNS_REASONS:
        return StepOutcome.STOPPED_BY_MONITOR
    return StepOutcome.FAILED_ERROR if result.is_error else StepOutcome.PASSED


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
        prompt = self._prompt(spec)
        root = Path(spec.handle.root)
        out_file = root / OUT_DIR / f"{spec.stage_id}.json"
        out_file.unlink(missing_ok=True)  # never read a previous attempt's outputs
        transcript = step.log_path.with_name(TRANSCRIPT_NAME)
        transcript.unlink(missing_ok=True)
        request = HarnessRequest(
            prompt=prompt, cwd=root, model=route.model, max_turns=self._settings.step_default(StepKind.AGENT).max_turns
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
        self._finish(step, result, out_file)

    @staticmethod
    def _finish(step: StepContext, result: HarnessResult, out_file: Path) -> None:
        step.log(f"stop reason {result.stop_reason}, {result.num_turns} turns, exit code {result.exit_code}")
        outcome, summary = _outcome(result), result.final_message
        outputs: dict[str, Any] = {}
        if out_file.exists():
            try:
                outputs = _read_outputs(out_file)
            except (ValueError, OSError) as exc:
                step.log(f"bad outputs file {out_file.name}: {exc}")
                if outcome == StepOutcome.PASSED:
                    outcome, summary = StepOutcome.FAILED_ERROR, f"invalid {OUT_DIR}/{out_file.name}: {exc}"
            finally:
                out_file.unlink(missing_ok=True)
        step.finish(outcome, summary, outputs, result.usage)

    def _harness(self, name: str) -> Harness:
        harness = self._harnesses.get(name)
        if harness is None:
            msg = f"harness {name!r} is not available on this worker; known: {', '.join(sorted(self._harnesses))}"
            raise ApplicationError(msg, non_retryable=True)
        return harness

    @staticmethod
    def _prompt(spec: AgentStepInput) -> str:
        repos = spec.handle.repos
        name = spec.home_repo or next(iter(repos), None)
        if name is None or name not in repos:
            msg = f"home repository {name!r} is not in the workspace"
            raise ApplicationError(msg, non_retryable=True)
        try:
            return render_prompt(
                Path(repos[name]), spec.prompt_name, spec.task, spec.context, spec.handle.root, spec.stage_id
            )
        except PromptError as exc:
            raise _non_retryable(exc) from exc


def _read_outputs(path: Path) -> dict[str, Any]:
    """Read the step's outputs file, a JSON object. Raises `ValueError` for anything else."""
    size = path.stat().st_size
    if size > OUTPUTS_MAX_BYTES:
        msg = f"{size} bytes is over the {OUTPUTS_MAX_BYTES} byte limit; put large data in an artifact"
        raise ValueError(msg)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        msg = f"expected a JSON object, got {type(data).__name__}"
        raise ValueError(msg)  # noqa: TRY004
    return data
