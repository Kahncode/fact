"""
The person in the loop: gates (an update), the status query and steering (a signal), as a mixin of `FactWorkflow`.

A gate waits on workflow state only, with no timer and no activity, so it costs nothing however long it stays open
and survives worker restarts. Its step (kind `gate`) is recorded through control activities, best effort: an API
outage must not lose a person's answer.

`HumanSteps` uses the workflow's `input`, `next_attempt` and `_control`; `FactWorkflow` calls its `__init__`.
"""

from typing import TYPE_CHECKING, Any

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from fact.core.activity_contract import (
        RECORD_GATE_ANSWERED,
        RECORD_GATE_OPENED,
        GateAnsweredInput,
        GateOpenedInput,
    )
    from fact.core.enums import StepOutcome
    from fact.core.gates import (
        ANSWER_GATE,
        STATUS,
        STEER,
        STEER_MAX_CHARS,
        GateAnswer,
        GateDecision,
        OpenGate,
        StageOutcome,
        WorkflowStatus,
    )

if TYPE_CHECKING:
    from fact.sdk.workflow import FactWorkflow

STEERING_HEADING = "## Additional instructions"


class HumanSteps:
    def __init__(self) -> None:
        self._open_gates: dict[str, OpenGate] = {}
        self._answers: dict[str, GateAnswer] = {}  # gates answered, by id; a closed gate never opens again
        self._steering: list[str] = []
        self._stage: str | None = None
        self._stage_attempt: int | None = None
        self._outcomes: list[StageOutcome] = []

    async def gate(self: FactWorkflow, gate_id: str, kind: str, details: dict[str, Any] | None = None) -> GateAnswer:
        """
        Open a gate and wait for a person to answer it through the `answer_gate` update; return the answer.

        `gate_id` is unique within the run. The gate's step is stage `kind`, numbered like any other stage.
        """
        if gate_id in self._open_gates or gate_id in self._answers:
            msg = f"gate {gate_id!r} was already opened in this run"
            raise ValueError(msg)
        gate = OpenGate(
            gate_id=gate_id,
            kind=kind,
            stage_id=kind,
            attempt=self.next_attempt(kind),
            details=details or {},
            opened_at=workflow.now(),
        )
        self._open_gates[gate_id] = gate
        workflow.logger.info("gate %s (%s) is open", gate_id, kind)
        step_id = await self._record_gate(RECORD_GATE_OPENED, self._opened_input(gate), str)
        await workflow.wait_condition(lambda: gate_id in self._answers)
        answer = self._answers[gate_id]
        outcome = StepOutcome.PASSED if answer.decision == GateDecision.APPROVE else StepOutcome.NEEDS_HUMAN
        self.note_outcome(gate.stage_id, gate.attempt, outcome)
        await self._record_gate(RECORD_GATE_ANSWERED, self._answered_input(gate, answer, step_id, outcome), type(None))
        return answer

    @workflow.update(name=ANSWER_GATE)
    def answer_gate(self, answer: GateAnswer) -> None:
        """Answer an open gate: approve or reject, with a note. The validator refuses any other gate id."""
        if self._open_gates.pop(answer.gate_id, None) is None:  # validated, but another answer got there first
            raise ApplicationError(self._not_open(answer.gate_id))
        self._answers[answer.gate_id] = answer

    @answer_gate.validator
    def _validate_answer(self, answer: GateAnswer) -> None:
        if answer.gate_id not in self._open_gates:
            raise ValueError(self._not_open(answer.gate_id))

    @workflow.query(name=STATUS)
    def status(self) -> WorkflowStatus:
        gate = next(iter(self._open_gates.values()), None)
        return WorkflowStatus(stage=self._stage, attempt=self._stage_attempt, gate=gate, outcomes=list(self._outcomes))

    @workflow.signal(name=STEER)
    def steer(self, text: str) -> None:
        """Queue instructions for the next agent session; a running session is never interrupted."""
        if text.strip():
            self._steering.append(text.strip()[:STEER_MAX_CHARS])

    def with_steering(self, context: str) -> str:
        """Append the queued instructions to an agent session's context and clear them."""
        if not self._steering:
            return context
        extra = "\n\n".join(self._steering)[-STEER_MAX_CHARS:]  # the newest, if many were queued
        self._steering.clear()
        return f"{context}\n\n{STEERING_HEADING}\n\n{extra}".lstrip()

    def note_stage(self, stage_id: str, attempt: int) -> None:
        self._stage, self._stage_attempt = stage_id, attempt

    def note_outcome(self, stage_id: str, attempt: int, outcome: StepOutcome) -> None:
        self._outcomes.append(StageOutcome(stage_id=stage_id, attempt=attempt, outcome=outcome))

    def _not_open(self, gate_id: str) -> str:
        if gate_id in self._answers:
            return f"gate {gate_id!r} is already closed: answered {self._answers[gate_id].decision}"
        open_ids = ", ".join(self._open_gates) or "none"
        return f"unknown gate {gate_id!r}; open gates: {open_ids}"

    def _opened_input(self: FactWorkflow, gate: OpenGate) -> GateOpenedInput:
        return GateOpenedInput(
            run_id=self.input.run_id,
            stage_id=gate.stage_id,
            attempt=gate.attempt,
            gate_id=gate.gate_id,
            kind=gate.kind,
            details=gate.details,
            opened_at=gate.opened_at,
        )

    def _answered_input(
        self: FactWorkflow, gate: OpenGate, answer: GateAnswer, step_id: str | None, outcome: StepOutcome
    ) -> GateAnsweredInput:
        answered_at = workflow.now()
        return GateAnsweredInput(
            run_id=self.input.run_id,
            step_id=step_id,
            stage_id=gate.stage_id,
            attempt=gate.attempt,
            gate_id=gate.gate_id,
            kind=gate.kind,
            details=gate.details,
            decision=answer.decision,
            note=answer.note,
            outcome=outcome,
            opened_at=gate.opened_at,
            answered_at=answered_at,
            wait_seconds=max((answered_at - gate.opened_at).total_seconds(), 0.0),
        )

    async def _record_gate[T](
        self: FactWorkflow, name: str, arg: GateOpenedInput | GateAnsweredInput, result_type: type[T]
    ) -> T | None:
        """Best effort: the gate holds the run either way; a missing step only leaves the UI without it."""
        try:
            return await self._control(name, arg, result_type)
        except ActivityError as exc:
            workflow.logger.warning("%s failed for gate %s: %s", name, arg.gate_id, exc.cause or exc)
            return None
