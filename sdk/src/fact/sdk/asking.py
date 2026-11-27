"""
Asking on the task, as a mixin of `FactWorkflow`: a blocked agent's question goes to a person, whose answer resumes it.

An agent session that answers `blocked` with a `question` opens a `needs_human` gate: the question is posted on the
issue (`gate_opened`, which adds `fact:needs-human`), saying to answer by mentioning the App (`@<slug>`). While the
gate is open, `wait_for_answer` polls the issue (every 60 s, heartbeating) for maintainers' comments mentioning the App
since the question; each time new ones arrive, an `answer` session (`converse`, prompt `answer`) reads the question and
every such comment since and decides: `continue` answers the gate (approve, `fact:needs-human` removed) and the blocked
session is resumed with its `instructions` and the thread; `ask` posts its `reply` and waits again; `stop` answers the
gate (reject), and the step stays `needs_human`. `fact gate approve|reject` answers the gate too, and wins: the wait is
cancelled and no `answer` session starts after it; its note is the instructions. A file task (no issue), or an issue
fact cannot watch (no App, a permanent GitHub error, the wait's 30 days spent), waits on `answer_gate` only.

`AskSteps` uses the workflow's `input`, `converse`, `open_gate`, `close_gate`, `answer_own_gate`, `gate_answered` and
`_activity`; `AgentSteps.agent` calls `ask_person`.
"""

import asyncio
import contextlib
import re
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field
from temporalio import workflow
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from fact.core.activity_contract import APP_SLUG, WAIT_FOR_ANSWER, AppSlugInput, Comment, WaitForAnswerInput
    from fact.core.enums import StepOutcome
    from fact.core.gates import NOTE_MAX_CHARS, GateAnswer, GateDecision, OpenGate
    from fact.core.types import SUMMARY_MAX_BYTES, StepResult, limit_text
    from fact.sdk.converse import comments_text
    from fact.sdk.failures import CONTEXT_MAX_BYTES

if TYPE_CHECKING:
    from fact.sdk.workflow import FactWorkflow

QUESTION_KIND = "needs_human"  # the gate's kind, and its stage
ANSWER_STAGE = "answer"
ANSWER_DECISIONS = ("continue", "ask", "stop")
_ISSUE_KEY = re.compile(r"gh:([^/#\s]+/[^/#\s]+)#(\d+)")


class Answered(BaseModel):
    """How a person answered a question: the gate's answer (its note: the instructions) and the thread it read."""

    model_config = ConfigDict(frozen=True)

    answer: GateAnswer
    thread: list[Comment] = Field(default_factory=list)


def question_of(result: StepResult) -> str | None:
    """Return the question of a `blocked` agent answer, or None when it asks none."""
    question = result.outputs.get("question")
    if result.outcome != StepOutcome.NEEDS_HUMAN or not isinstance(question, str) or not question.strip():
        return None
    return question.strip()


def resume_context(question: str, answered: Answered) -> str:
    """Build the context of the session resumed once a person answered its question."""
    instructions = answered.answer.note or "No instructions: carry on with what you know now."
    parts = [
        f"## A person answered your question\n\nYou asked:\n\n{_quote(question)}",
        f"### Instructions\n\n{instructions}",
    ]
    if answered.thread:
        parts.append(f"### The thread\n\n{comments_text(answered.thread)}")
    return limit_text("\n\n".join(parts), CONTEXT_MAX_BYTES)


def stopped_by(result: StepResult, question: str, answered: Answered) -> StepResult:
    """Return the blocked step, once a person stopped the run instead of answering its question."""
    note = f": {answered.answer.note}" if answered.answer.note else ""
    summary = limit_text(f"asked {question!r}; a person stopped the run{note}", SUMMARY_MAX_BYTES)
    return result.model_copy(update={"outcome": StepOutcome.NEEDS_HUMAN, "summary": summary})


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" for line in text.splitlines())


def _issue(key: str) -> tuple[str, int] | None:
    if m := _ISSUE_KEY.fullmatch(key):
        return m.group(1), int(m.group(2))
    return None


class AskSteps:
    def __init__(self) -> None:
        super().__init__()
        self._questions = 0  # questions asked so far, for unique gate ids

    async def ask_person(self: FactWorkflow, stage_id: str, question: str) -> Answered:
        """Ask `question` on the task (stage `stage_id` asked it) and wait for the answer."""
        self._questions += 1
        gate_id = f"{stage_id}-question-{self._questions}"
        issue = _issue(self.input.task.key)
        slug = await self._app_slug() if issue else None
        how = f"Answer by mentioning @{slug} in a comment here, or" if slug else "Answer"
        command = f"`fact gate {self.input.run_id} approve|reject`"
        need = f"the {stage_id} session asks:\n\n{_quote(question)}\n\n{how} with {command}"
        details = {"summary": f"{stage_id} asks: {question}", "question": question, "stage": stage_id}
        gate, step_id = await self.open_gate(gate_id, QUESTION_KIND, details, need)
        thread: list[Comment] = []
        if issue and slug:
            thread = await self._watch_issue(gate, issue, question)
        await workflow.wait_condition(lambda: self.gate_answered(gate_id))
        return Answered(answer=await self.close_gate(gate, step_id), thread=thread)

    async def _watch_issue(self: FactWorkflow, gate: OpenGate, issue: tuple[str, int], question: str) -> list[Comment]:
        """Run an `answer` session on each new mentioning comment until the gate is answered; return the thread."""
        repo, number = issue
        handled: list[int] = []
        thread: list[Comment] = []
        context = f"## The question\n\nA session of this run was blocked and asked on the issue:\n\n{_quote(question)}"
        while not self.gate_answered(gate.gate_id):
            spec = WaitForAnswerInput(
                run_id=self.input.run_id, repo=repo, number=number, since=gate.opened_at, handled=handled
            )
            new = await self._wait_or_answered(gate.gate_id, spec)
            if new is None:  # answered through `answer_gate`, or the issue cannot be watched
                return thread
            handled = [*handled, *(c.id for c in new)]
            conversation = await self.converse(
                ANSWER_STAGE,
                "answer",
                repo=repo,
                number=number,
                decisions=ANSWER_DECISIONS,
                pull_request=False,
                since=gate.opened_at,
                mentioning_app=True,
                context=context,
            )
            thread = conversation.comments or thread
            handled = list(dict.fromkeys([*handled, *(c.id for c in conversation.comments)]))
            if conversation.answer is not None and conversation.decision != "ask":
                decision = GateDecision.APPROVE if conversation.decision == "continue" else GateDecision.REJECT
                note = conversation.answer.instructions[:NOTE_MAX_CHARS]
                self.answer_own_gate(GateAnswer(gate_id=gate.gate_id, decision=decision, note=note))
        return thread

    async def _wait_or_answered(self: FactWorkflow, gate_id: str, spec: WaitForAnswerInput) -> list[Comment] | None:
        """Wait for new mentioning comments, or for the gate's answer, which wins: then None (the wait cancelled)."""
        comments = asyncio.ensure_future(self._activity(WAIT_FOR_ANSWER, spec, list[Comment]))
        answered = asyncio.ensure_future(workflow.wait_condition(lambda: self.gate_answered(gate_id)))
        try:
            await asyncio.wait([comments, answered], return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (comments, answered):
                task.cancel()
        if self.gate_answered(gate_id):
            with contextlib.suppress(asyncio.CancelledError, ActivityError):
                await comments
            return None
        try:
            return comments.result()
        except ActivityError as exc:  # no App, a permanent GitHub error, or 30 days: only `fact gate` answers now
            workflow.logger.warning("watching the issue failed, waiting on the gate only: %s", exc.cause or exc)
            return None

    async def _app_slug(self: FactWorkflow) -> str | None:
        """Return the App's slug, to mention; None when it cannot be read (then only `fact gate` answers)."""
        try:
            return await self._activity(APP_SLUG, AppSlugInput(run_id=self.input.run_id), str)
        except ActivityError as exc:
            workflow.logger.warning("reading the App's slug failed: %s", exc.cause or exc)
            return None
