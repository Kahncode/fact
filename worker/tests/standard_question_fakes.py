"""The asking side of the `Standard` test fakes: the App's slug and the wait for answers on the issue, scripted."""

import asyncio
from dataclasses import dataclass, field

from fact.core.activity_contract import APP_SLUG, WAIT_FOR_ANSWER, AppSlugInput, Comment, WaitForAnswerInput
from fact.core.enums import StepOutcome
from fact.core.types import StepResult
from temporalio import activity

SLUG = "sofact-dev"
HEARTBEAT_S = 0.05


def blocked(question: str = "", session: str = "s-1") -> StepResult:
    """Return a blocked implement session, asking `question` (none if empty)."""
    outputs = {"status": "blocked", "summary": "stuck", "blocker": "which API?"}
    if question:
        outputs["question"] = question
    return StepResult(outcome=StepOutcome.NEEDS_HUMAN, summary="which API?", outputs=outputs, session_id=session)


def answer(decision: str, reply: str = "", instructions: str = "") -> StepResult:
    """Return an `answer` session's result."""
    outputs = {
        "status": "done",
        "summary": decision,
        "decision": decision,
        "reply": reply,
        "instructions": instructions,
    }
    return StepResult(outcome=StepOutcome.PASSED, summary=decision, outputs=outputs)


@dataclass
class FakeQuestionSteps:
    """`wait_for_answer` returns each of `issue_answers` in turn; once empty, it waits until cancelled, heartbeating."""

    issue_answers: list[list[Comment]] = field(default_factory=list)
    answers: list[StepResult] = field(default_factory=list)  # each `answer` session, in order

    @activity.defn(name=APP_SLUG)
    async def app_slug(self, spec: AppSlugInput) -> str:
        self._call(APP_SLUG, spec)  # type: ignore[attr-defined] - from `FakePRSteps`
        return SLUG

    @activity.defn(name=WAIT_FOR_ANSWER)
    async def wait_for_answer(self, spec: WaitForAnswerInput) -> list[Comment]:
        self._call(WAIT_FOR_ANSWER, spec)  # type: ignore[attr-defined] - from `FakePRSteps`
        if self.issue_answers:
            return self.issue_answers.pop(0)
        while True:  # nobody answers on the issue: only a cancellation ends it
            activity.heartbeat()
            await asyncio.sleep(HEARTBEAT_S)

    def next_answer(self) -> StepResult:
        return self.answers.pop(0) if self.answers else answer("ask", reply="Could you say more?")
