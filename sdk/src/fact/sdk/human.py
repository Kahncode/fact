"""
The person in the loop as a mixin of `FactWorkflow`: gates (an update), status (a query), steering, notifications.

A gate waits on workflow state only, with no timer and no activity, so it costs nothing however long it stays open
and survives worker restarts. Its step (kind `gate`) is recorded through control activities, best effort: an API
outage must not lose a person's answer. Opening a gate also creates a `gate_waiting` notification, once per gate, and
reports `gate_opened` to the task's source (`fact:needs-human`); answering it reports `gate_answered`.

`HumanSteps` uses the workflow's `input`, `next_attempt`, `_activity` and `report_task`; `FactWorkflow` calls its
`__init__`, which calls the next mixin's.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from fact.core.activity_contract import (
        NOTIFY,
        RECORD_GATE_ANSWERED,
        RECORD_GATE_OPENED,
        GateAnsweredInput,
        GateOpenedInput,
        NotifyInput,
    )
    from fact.core.enums import NotificationKind, RunOutcome, StepOutcome
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
    from fact.core.types import PRRef, RunResult

if TYPE_CHECKING:
    from fact.sdk.workflow import FactWorkflow

STEERING_HEADING = "## Additional instructions"
TITLE_MAX_CHARS = 300
KEY_MAX_CHARS = 300  # `NotifyInput` refuses longer: a ValidationError would fail the workflow task
BAD_ENDS = frozenset({RunOutcome.FAILED, RunOutcome.NEEDS_HUMAN})


class HumanSteps:
    def __init__(self) -> None:
        super().__init__()
        self._open_gates: dict[str, OpenGate] = {}
        self._answers: dict[str, GateAnswer] = {}  # gates answered, by id; a closed gate never opens again
        self._steering: list[str] = []
        self._stage: str | None = None
        self._stage_attempt: int | None = None
        self._outcomes: list[StageOutcome] = []
        self._waiting: str | None = None

    async def gate(self: FactWorkflow, gate_id: str, kind: str, details: dict[str, Any] | None = None) -> GateAnswer:
        """
        Open a gate and wait for a person to answer it through the `answer_gate` update; return the answer.

        `gate_id` is unique within the run. The gate's step is stage `kind`, numbered like any other stage.
        """
        gate, step_id = await self.open_gate(gate_id, kind, details)
        await workflow.wait_condition(lambda: gate_id in self._answers)
        return await self.close_gate(gate, step_id)

    async def open_gate(
        self: FactWorkflow, gate_id: str, kind: str, details: dict[str, Any] | None = None, need: str = ""
    ) -> tuple[OpenGate, str | None]:
        """
        Open a gate without waiting: record its step, notify, report `gate_opened`; return it and its step id.

        `need` is the task comment's text (by default the summary and how to answer with `fact gate`). Wait until
        `gate_answered(gate_id)`, then call `close_gate`.
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
        await self._notify_gate(gate)
        await self.report_task("gate_opened", detail=need or self._gate_need(gate))
        return gate, step_id

    def gate_answered(self, gate_id: str) -> bool:
        return gate_id in self._answers

    def answer_own_gate(self, answer: GateAnswer) -> None:
        """Answer an open gate from the workflow itself (a person answered elsewhere, such as on the issue)."""
        if self._open_gates.pop(answer.gate_id, None) is not None:
            self._answers[answer.gate_id] = answer

    async def close_gate(self: FactWorkflow, gate: OpenGate, step_id: str | None) -> GateAnswer:
        """Record the answer of an answered gate and report `gate_answered`; return the answer."""
        answer = self._answers[gate.gate_id]
        outcome = StepOutcome.PASSED if answer.decision == GateDecision.APPROVE else StepOutcome.NEEDS_HUMAN
        self.note_outcome(gate.stage_id, gate.attempt, outcome)
        await self._record_gate(RECORD_GATE_ANSWERED, self._answered_input(gate, answer, step_id, outcome), type(None))
        await self.report_task("gate_answered")
        return answer

    async def notify(
        self: FactWorkflow, kind: NotificationKind, title: str, body: str = "", link: str | None = None, key: str = ""
    ) -> None:
        """
        Tell a person through a notification, best effort: a failed one is logged, and the run goes on.

        One notification per run and `key` (`kind` if empty): calling again with the same one changes nothing.
        """
        run_id = self.input.run_id
        spec = NotifyInput(
            run_id=run_id,
            kind=kind,
            title=title[:TITLE_MAX_CHARS],
            body=body,
            link=link or self.input.run_url or None,
            dedupe_key=f"{run_id}:{key or kind}"[:KEY_MAX_CHARS],
        )
        try:
            await self._activity(NOTIFY, spec, int)
        except ActivityError as exc:
            workflow.logger.warning("notify failed for %s: %s", spec.dedupe_key, exc.cause or exc)

    async def notify_run_end(self: FactWorkflow, result: RunResult) -> None:
        """
        Notify how the run ended: `prs_ready` with PRs open, `run_failed` if it failed or needs a person.

        A run that waited for its PRs to merge or close was told `prs_ready` before that wait; it ends `run_finished`.
        A change set fact merged itself (`merged_by_fact`) needs no one, so its `merged` end sends nothing.
        """
        if result.outcome == RunOutcome.MERGED and self.merged_by_fact:
            return
        key = self.input.task.key
        link = result.prs[0].url if result.prs else None
        if result.outcome == RunOutcome.PR_OPENED:
            await self.notify_prs_ready(result.prs, result.summary)
            return
        if result.outcome in BAD_ENDS:
            kind, title = NotificationKind.RUN_FAILED, f"{key} ended {result.outcome}"
        else:
            kind, title = NotificationKind.RUN_FINISHED, f"{key} ended {result.outcome}"
        await self.notify(kind, title, result.summary, link)

    async def notify_prs_ready(
        self: FactWorkflow, prs: Sequence[PRRef], body: str = "", reasons: Sequence[str] = ()
    ) -> None:
        """Tell a person the PRs are ready for review (`prs_ready`), once per run, with why they need one."""
        title = f"{self.input.task.key}: {len(prs) or 'no'} PR(s) ready for review"
        link = prs[0].url if prs else None
        body = body or ", ".join(pr.url for pr in prs)
        if reasons:
            body += "\n\nWhy a review:\n" + "\n".join(f"- {r}" for r in reasons)
        await self.notify(NotificationKind.PRS_READY, title, body, link)

    def _gate_need(self: FactWorkflow, gate: OpenGate) -> str:
        """Say what the gate needs, for the task's comment: its summary, and how to answer."""
        need = str(gate.details.get("summary", "")) or f"gate {gate.gate_id} ({gate.kind})"
        return f"{need} (answer with `fact gate {self.input.run_id} approve|reject`)"

    async def _notify_gate(self: FactWorkflow, gate: OpenGate) -> None:
        summary = str(gate.details.get("summary", ""))
        answer = f"Answer with `fact gate {self.input.run_id} approve|reject`."
        body = f"{summary}\n\n{answer}" if summary else answer
        title = f"{self.input.task.key} waits for you at gate {gate.gate_id} ({gate.kind})"
        await self.notify(NotificationKind.GATE_WAITING, title, body, key=f"gate:{gate.gate_id}")

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
        return WorkflowStatus(
            stage=self._stage,
            attempt=self._stage_attempt,
            gate=gate,
            waiting=self._waiting,
            outcomes=list(self._outcomes),
        )

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

    def note_waiting(self, reason: str | None) -> None:
        """Say why the run waits without a gate (the `status` query's `waiting`); None once it goes on."""
        self._waiting = reason

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
            return await self._activity(name, arg, result_type)
        except ActivityError as exc:
            workflow.logger.warning("%s failed for gate %s: %s", name, arg.gate_id, exc.cause or exc)
            return None
