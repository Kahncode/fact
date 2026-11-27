"""
Waiting for review as a mixin of `FactWorkflow`, GitHub's review flow (`07-reference-workflow.md`).

`until_reviewed` follows every PR of the change set until each is merged or closed, handing each approval and each
review requesting changes to the workflow; a PR fact merges is seen merged by the next wait. `wait_for_review` waits until something happens on the open PRs and returns it, at most one event per PR: `merged`,
`closed`, `approved` (an `APPROVED` review on the PR's head) or `changes_requested` (a `CHANGES_REQUESTED` review).
Every review it returns is added to `handled_reviews`, workflow state that survives worker restarts, so the same
review never starts a second round or a second merge attempt. All of a run's waits share `REVIEW_TIMEOUT` (30 days).
`review_reply` runs one round's conversation (`converse` with the `review_reply` prompt) over the review and every
comment since fact last pushed or last replied; `request_review` asks the reviewer to look again.

`ReviewWaitSteps` uses the workflow's `input`, `next_attempt`, `note_outcome`, `pushed_at`, `converse`, `_activity`
and the gate recording helpers of `HumanSteps`.
"""

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from temporalio import workflow
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from fact.core.activity_contract import (
        RECORD_GATE_ANSWERED,
        RECORD_GATE_OPENED,
        REQUEST_REVIEW,
        REVIEW_TIMEOUT,
        WAIT_FOR_REVIEW,
        RequestReviewInput,
        ReviewEvent,
        WaitForReviewInput,
    )
    from fact.core.enums import RunOutcome, StepOutcome
    from fact.core.gates import NOTE_MAX_CHARS, GateAnswer, GateDecision, OpenGate
    from fact.core.types import PRRef, RunResult, limit_text
    from fact.sdk.converse import Conversation, comments_text
    from fact.sdk.failures import CONTEXT_MAX_BYTES

if TYPE_CHECKING:
    from fact.sdk.workflow import FactWorkflow

REVIEW_STAGE = "wait_for_review"
REVIEW_REPLY_STAGE = "review_reply"
REVIEW_DECISIONS = ("implement", "reply")
MIN_WAIT = timedelta(minutes=1)  # once the 30 days are spent, the next wait times out after this and fails the run
PASSING_EVENTS = frozenset({"merged", "approved"})


class ReviewWaitSteps:
    handled_reviews: list[int]  # review ids `wait_for_review` returned: never acted on twice
    _review_deadline: datetime | None  # when the run's 30 days of review end, from its first wait
    _replied_at: datetime | None  # when the last review round read its comments

    async def until_reviewed(
        self: FactWorkflow, prs: Sequence[PRRef], on_review: Callable[[ReviewEvent], Awaitable[RunResult | None]]
    ) -> RunResult:
        """
        Wait for review until every PR of `prs` is merged or closed: `merged` if all were merged, else `closed`.

        `on_review` acts on an `approved` or `changes_requested` event: None to wait again, or the run's end. The PRs
        are waited for at their latest pushed head (`prs`); with no PR there is nothing to wait for: `pr_opened`.
        """
        keys = {(pr.repo, pr.number) for pr in prs}
        ended: dict[tuple[str, int], str] = {}
        waiting = list(prs)
        while waiting:
            for event in await self.wait_for_review(waiting):
                if event.kind in {"merged", "closed"}:
                    ended[event.pr.repo, event.pr.number] = event.kind
                elif (end := await on_review(event)) is not None:
                    return end
            waiting = [pr for pr in self.prs if (pr.repo, pr.number) in keys - ended.keys()]
        current = [pr for pr in self.prs if (pr.repo, pr.number) in keys] or list(prs)
        summary = ", ".join(pr.url for pr in current)
        if not ended:
            return RunResult(outcome=RunOutcome.PR_OPENED, prs=current, summary=summary)
        merged = all(state == "merged" for state in ended.values())
        return RunResult(outcome=RunOutcome.MERGED if merged else RunOutcome.CLOSED, prs=current, summary=summary)

    async def wait_for_review(
        self: FactWorkflow, prs: Sequence[PRRef], stage_id: str = REVIEW_STAGE
    ) -> list[ReviewEvent]:
        """
        Wait until something happens on one of `prs` (each still open) and return what, in `prs` order.

        The wait is a gate-like step (stage `stage_id`, kind `gate`), one per call: the run shows as waiting, and all
        of the step's time is human wait time. Nobody answers it through `answer_gate`; GitHub does. If the wait fails
        (the run's 30 days pass, or GitHub refuses), its step is ended `needs_human` and the error raised.
        """
        attempt = self.next_attempt(stage_id)
        urls = [pr.url for pr in prs]
        details = {"summary": f"Review, approve, merge or close: {', '.join(urls)}", "prs": urls}
        gate = OpenGate(
            gate_id=f"{stage_id}-{attempt}",
            kind=stage_id,
            stage_id=stage_id,
            attempt=attempt,
            details=details,
            opened_at=workflow.now(),
        )  # never in `_open_gates`, so `answer_gate` cannot answer it
        step_id = await self._record_gate(RECORD_GATE_OPENED, self._opened_input(gate), str)
        if self._review_deadline is None:
            self._review_deadline = workflow.now() + REVIEW_TIMEOUT
        left = max(self._review_deadline - workflow.now(), MIN_WAIT)
        spec = WaitForReviewInput(run_id=self.input.run_id, prs=list(prs), handled_reviews=list(self.handled_reviews))
        try:
            events = await self._activity(WAIT_FOR_REVIEW, spec, list[ReviewEvent], max_duration=left)
        except ActivityError as exc:
            await self._end_review(gate, step_id, GateDecision.REJECT, f"waiting failed: {exc.cause or exc}")
            raise
        self.handled_reviews += [e.review_id for e in events if e.review_id is not None]
        passed = all(e.kind in PASSING_EVENTS for e in events)
        note = ", ".join(f"{e.pr.url}: {e.kind}" for e in events)
        await self._end_review(gate, step_id, GateDecision.APPROVE if passed else GateDecision.REJECT, note)
        return events

    async def review_reply(
        self: FactWorkflow, event: ReviewEvent, stage_id: str = REVIEW_REPLY_STAGE, prompt: str = "review_reply"
    ) -> Conversation:
        """
        Answer a review requesting changes with one read-only session; its decision is `implement` or `reply`.

        The session reads the review and every comment since fact last pushed (or last replied); its replies are
        posted, and `implement` comes with `instructions` for the implement session.
        """
        since = max((t for t in (self.pushed_at, self._replied_at) if t is not None), default=None)
        self._replied_at = workflow.now()
        pr = event.pr
        context = (
            f"## The review\n\nReview {event.review_id} by @{event.reviewer} requested changes on {pr.url} "
            f"(head `{pr.head_sha}`). Its comments, and every comment on the PR since fact last pushed, are below."
        )
        return await self.converse(
            stage_id,
            prompt,
            repo=pr.repo,
            number=pr.number,
            decisions=REVIEW_DECISIONS,
            since=since,
            review_id=event.review_id,
            context=context,
        )

    async def request_review(self: FactWorkflow, pr: PRRef, reviewers: Sequence[str]) -> None:
        """Ask `reviewers` to review the PR again, best effort: a failure is logged and the run goes on."""
        spec = RequestReviewInput(run_id=self.input.run_id, pr=pr, reviewers=[r for r in reviewers if r])
        try:
            await self._activity(REQUEST_REVIEW, spec, type(None))
        except ActivityError as exc:
            workflow.logger.warning("request_review failed for %s: %s", pr.url, exc.cause or exc)

    async def _end_review(
        self: FactWorkflow, gate: OpenGate, step_id: str | None, decision: GateDecision, note: str
    ) -> None:
        outcome = StepOutcome.PASSED if decision == GateDecision.APPROVE else StepOutcome.NEEDS_HUMAN
        answer = GateAnswer(gate_id=gate.gate_id, decision=decision, note=note[:NOTE_MAX_CHARS])
        self.note_outcome(gate.stage_id, gate.attempt, outcome)
        answered = self._answered_input(gate, answer, step_id, outcome)
        await self._record_gate(RECORD_GATE_ANSWERED, answered, type(None))


def rework_context(event: ReviewEvent, conversation: Conversation) -> str:
    """Build the `$context` of the implement session a review round's `implement` decision starts."""
    instructions = conversation.answer.instructions if conversation.answer else ""
    intro = (
        f"## Review round\n\n@{event.reviewer} requested changes on {event.pr.url}. The change is already on the "
        "branch and in the open PR: change it as asked, on top of what is there, and commit."
    )
    parts = [
        intro,
        f"### What to change\n\n{instructions or 'See the review comments below.'}",
        f"### Review comments\n\n{comments_text(conversation.comments)}",
    ]
    return limit_text("\n\n".join(parts), CONTEXT_MAX_BYTES)
