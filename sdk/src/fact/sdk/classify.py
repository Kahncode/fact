"""
Classify as a mixin of `FactWorkflow`: route a change set whose CI is green to `auto_merge` or `human_review`.

`classify_signals` raises the rules' signals, a read-only agent session (`.fact/prompts/classify.md`) rates risk, door
and security, `fact.sdk.classification.decide` combines them, and `label_change_set` puts `fact:auto-merge` or
`fact:needs-review` on each PR and a `## Review` section in its description; that step's outputs hold the
classification. Signals that cannot be read, or a session with no valid answer, route to `human_review`.
`route_to_review` relabels a change set `fact:needs-review` when fact does not merge it after all (auto-merge off for
the project, or GitHub refused the merge), adding why to its reasons.

`ClassifySteps` uses the workflow's `input`, `handle`, `assumptions`, `spec_ambiguous`, `_change_set`, `agent`,
`next_attempt`, `note_outcome` and `_activity`.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING

from temporalio import workflow
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from fact.core.activity_contract import (
        CLASSIFY_SIGNALS,
        LABEL_CHANGE_SET,
        ClassifySignalsInput,
        LabelChangeSetInput,
    )
    from fact.core.classification import Classification, ClassifySignal
    from fact.core.enums import StepOutcome
    from fact.core.types import PRRef
    from fact.sdk.classification import ClassifyAnswer, classify_context, decide, parse_classify
    from fact.sdk.review import Review

if TYPE_CHECKING:
    from fact.sdk.workflow import FactWorkflow

CLASSIFY_STAGE = "classify"
LABEL_STAGE = "label_prs"


class ClassifySteps:
    async def classify(
        self: FactWorkflow,
        prs: Sequence[PRRef],
        review: Review | None = None,
        *,
        prompt: str = "classify",
        stage_id: str = CLASSIFY_STAGE,
    ) -> Classification:
        """Classify the open change set from the rules' signals and one read-only session, then label its PRs."""
        signals, problems = await self._classify_signals()
        context = classify_context(review, signals, self.assumptions, spec_ambiguous=self.spec_ambiguous)
        answer: ClassifyAnswer | str
        try:
            answer = parse_classify(await self.agent(stage_id, prompt, context, pr_text=False, ask=False))
        except ActivityError as exc:  # timed out, interrupted twice, or the step could not start
            answer = f"the classify session failed: {exc.cause or exc}"
        forced = self.spec_ambiguous and self.input.review_spec_ambiguous  # else only the session weighs it
        classification = decide(signals, answer, spec_ambiguous=forced, problems=problems)
        workflow.logger.info("classified %s: %s", classification.route, "; ".join(classification.reasons))
        await self.label_change_set(prs, classification)
        return classification

    async def route_to_review(
        self: FactWorkflow, prs: Sequence[PRRef], classification: Classification, reason: str
    ) -> Classification:
        """Send a change set fact will not merge to `human_review` after all: relabel its PRs, `reason` added."""
        reasons = [*classification.reasons, reason]
        review = classification.model_copy(update={"route": "human_review", "reasons": reasons})
        await self.label_change_set(prs, review)
        return review

    async def _classify_signals(self: FactWorkflow) -> tuple[list[ClassifySignal], list[str]]:
        run = self.input
        spec = ClassifySignalsInput(run_id=run.run_id, project=run.project, task_key=run.task.key, handle=self.handle)
        try:
            return await self._activity(CLASSIFY_SIGNALS, spec, list[ClassifySignal]), []
        except ActivityError as exc:
            return [], [f"the rules' signals could not be read: {exc.cause or exc}"]

    async def label_change_set(
        self: FactWorkflow, prs: Sequence[PRRef], classification: Classification, stage_id: str = LABEL_STAGE
    ) -> None:
        """Label each PR with its route and re-render its description with `## Review`; later pushes keep it."""
        if self._change_set is None:
            msg = "no change set: call `open_change_set` first"
            raise RuntimeError(msg)
        update = {
            "classification": classification,
            "assumptions": list(self.assumptions),
            "spec_ambiguous": self.spec_ambiguous,
        }
        self._change_set = self._change_set.model_copy(update=update)
        spec = LabelChangeSetInput(
            run_id=self.input.run_id,
            change_set=self._change_set,
            prs=list(prs),
            stage_id=stage_id,
            attempt=self.next_attempt(stage_id),
        )
        await self._activity(LABEL_CHANGE_SET, spec, type(None))
        self.note_outcome(stage_id, spec.attempt, StepOutcome.PASSED)
