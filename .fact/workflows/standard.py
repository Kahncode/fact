"""
Standard: from a task to a PR, then through review to the merge (`docs/spec/07-reference-workflow.md`).

An optional `approve_plan` gate; prepare and set up the workspace; implement; loop lint and test with fix sessions;
review against the acceptance criteria, `address_review` if it asks for changes; `learn`; open the PR; loop CI with
`fix_ci` sessions, merging the base only when GitHub says the PR conflicts or is out of date. Once CI is green,
`classify`: a change set routed `auto_merge` in a project whose `auto_merge` is on is merged by fact, and the run ends
`merged` with no notification. Otherwise the PRs are labelled `fact:needs-review`, `prs_ready` is sent and each PR
follows GitHub's review flow (`until_reviewed`): an approval on its head merges it once CI is green there, whatever
`auto_merge` says (a push on the way needs a new approval); a review requesting changes gets a `review_reply` session
whose replies fact posts, and whose `implement` runs the chain again from implement on the same PR (never classified
again) while `reply` changes nothing; then the reviewer is asked again. The run ends once every PR is merged or
closed: `merged` if all were merged, else `closed`. A loop out of attempts (checks, base sync, CI) ends the run
`needs_human`, with no gate. An agent that is blocked with a `question` asks it on the task (`FactWorkflow.agent`,
`fact.sdk.asking`): a person's answer resumes it, and one who stops it ends the run `needs_human`, like an agent
blocked without a question; one that errors ends it `failed` (a `learn` session that does not pass keeps the earlier
PR text). `FactWorkflow` comments the run's cost on its PRs last.
"""

from temporalio import workflow
from temporalio.exceptions import ActivityError, FailureError

with workflow.unsafe.imports_passed_through():
    from fact.core.classification import Classification
    from fact.sdk.asking import ANSWER_STAGE, QUESTION_KIND
    from fact.sdk.base_sync import RESOLVE_STAGE, SYNC_STAGE
    from fact.sdk.classify import CLASSIFY_STAGE, LABEL_STAGE
    from fact.sdk.review import Review, parse_review, review_context
    from fact.sdk.review_wait import REVIEW_REPLY_STAGE, REVIEW_STAGE, rework_context
    from fact.sdk.types import (
        Check,
        GateDecision,
        PRRef,
        ReviewEvent,
        RunInput,
        RunOutcome,
        RunResult,
        StepKind,
        StepOutcome,
        StepResult,
        limit_text,
    )
    from fact.sdk.workflow import FactWorkflow, Stage, stages

CHECKS: list[Check] = ["lint", "test"]
MAX_FIX_ATTEMPTS = 3
MAX_CI_FIX_ATTEMPTS = 2
SUMMARY_BYTES = 1024
APPROVE_PLAN = "approve_plan"  # the plan approval gate's id, kind and stage
AUTO_MERGE_OFF = "auto-merge is off for this project"
MERGE_REFUSED = "GitHub refused the merge (such as branch protection requiring an approval, or CI not green)"
MAX_BASE_SYNCS = 2  # merges of the base per loop (a conflicting PR, a merge refused as out of date) before a person


def _ended(stage: str, step: StepResult, prs: list[PRRef] | None = None) -> RunResult:
    """End the run after an agent step that did not pass: `needs_human` if the agent said it was blocked."""
    outcome = RunOutcome.NEEDS_HUMAN if step.outcome == StepOutcome.NEEDS_HUMAN else RunOutcome.FAILED
    summary = limit_text(f"{stage} ended {step.outcome}: {step.summary}", SUMMARY_BYTES)
    return RunResult(outcome=outcome, prs=prs or [], summary=summary)


def _heads(prs: list[PRRef]) -> dict[tuple[str, int], str]:
    return {(pr.repo, pr.number): pr.head_sha for pr in prs}


@stages(
    [
        Stage(APPROVE_PLAN, "Approve plan", StepKind.GATE),
        Stage("prepare", "Prepare workspace", StepKind.SHELL),
        Stage("setup", "Set up workspace", StepKind.SHELL),
        Stage("implement", "Implement", StepKind.AGENT),
        Stage("checks", "Lint and test", StepKind.SHELL, loops_to="implement"),
        Stage("review", "Review", StepKind.AGENT),
        Stage("address_review", "Address review", StepKind.AGENT, loops_to="checks"),
        Stage("learn", "Learn", StepKind.AGENT),
        Stage("open_pr", "Open PR", StepKind.GITHUB),
        Stage("wait_for_ci", "Wait for CI", StepKind.GITHUB, loops_to="fix_ci"),
        Stage("fix_ci", "Fix CI", StepKind.AGENT, loops_to="open_pr"),
        Stage(CLASSIFY_STAGE, "Classify", StepKind.AGENT),
        Stage(LABEL_STAGE, "Label PRs", StepKind.GITHUB),
        Stage(SYNC_STAGE, "Merge base branch", StepKind.SHELL),
        Stage(RESOLVE_STAGE, "Resolve conflicts", StepKind.AGENT, loops_to=SYNC_STAGE),
        Stage(REVIEW_STAGE, "Wait for review", StepKind.GATE),
        Stage(REVIEW_REPLY_STAGE, "Reply to review", StepKind.AGENT, loops_to="implement"),
        Stage(QUESTION_KIND, "Ask a person", StepKind.GATE),
        Stage(ANSWER_STAGE, "Read the answer", StepKind.AGENT),
    ]
)
@workflow.defn(name="standard")
class Standard(FactWorkflow):
    @workflow.run
    async def run(self, run_input: RunInput) -> RunResult:
        try:
            result = await self.execute(run_input, self._steps)
        except FailureError as exc:
            summary = limit_text(f"the run failed: {exc.cause or exc}", SUMMARY_BYTES)
            await self.notify_run_end(RunResult(outcome=RunOutcome.FAILED, prs=self.prs, summary=summary))
            raise
        await self.notify_run_end(result)
        return result

    async def _steps(self) -> RunResult:
        if self.input.require_plan_approval and (stopped := await self._approve_plan()) is not None:
            return stopped
        return await self._to_pr()

    async def _to_pr(self) -> RunResult:
        await self.prepare()
        failed_setup = await self.setup()
        if failed_setup is not None:
            summary = f"setup ended {failed_setup.outcome}: {failed_setup.summary}"
            return RunResult(outcome=RunOutcome.FAILED, summary=limit_text(summary, SUMMARY_BYTES))
        done = await self._implemented()
        if isinstance(done, RunResult):
            return done
        implemented, review = done
        await self._learn()
        await self.mark_task_done()
        prs = await self.open_change_set(*self.pr_text(implemented.summary), criteria=review.criteria)
        return await self._ci_green(prs, review)

    async def _implemented(self, context: str = "") -> tuple[StepResult, Review] | RunResult:
        """Implement (with `context`), loop the checks, review and address it; the run's end if one of them stops it."""
        implemented = await self.agent("implement", "implement", context)
        if implemented.outcome != StepOutcome.PASSED:
            return _ended("implement", implemented, self.prs)
        if (stuck := await self._checks_green()) is not None:
            return stuck
        reviewed, review = parse_review(await self.agent("review", "review", pr_text=False))
        if review is None:
            return _ended("review", reviewed, self.prs)
        if (stuck := await self._address(review)) is not None:
            return stuck
        return implemented, review

    async def _approve_plan(self) -> RunResult | None:
        """Ask a person to approve the task as planned: None if approved, else the run's end, `stopped`."""
        task = self.input.task
        details = {
            "summary": f"Approve the plan for {task.key}: {task.title}",
            "title": task.title,
            "path": task.path,
            "acceptance_criteria": task.acceptance_criteria,
        }
        answer = await self.gate(APPROVE_PLAN, APPROVE_PLAN, details)
        if answer.decision == GateDecision.APPROVE:
            return None
        text = f"plan rejected: {answer.note}" if answer.note else "plan rejected"
        return RunResult(outcome=RunOutcome.STOPPED, summary=limit_text(text, SUMMARY_BYTES))

    async def _checks_green(self) -> RunResult | None:
        """Loop the checks with fix sessions; None once they are green, else the run's end, `needs_human`."""
        checks = await self.until_green(CHECKS, fix_prompt="fix_checks", max_attempts=MAX_FIX_ATTEMPTS)
        if checks == StepOutcome.PASSED:
            return None
        failed = ", ".join(c.name for c in self.check_results if c.outcome != StepOutcome.PASSED)
        return self._stuck(f"checks still failing after {MAX_FIX_ATTEMPTS} fix sessions: {failed}")

    async def _synced(self) -> RunResult | None:
        """Merge the base branch; once an agent resolved conflicts, loop the checks again. None when all is well."""
        synced = await self.sync_with_base(RESOLVE_STAGE)
        if synced == "resolved":
            return await self._checks_green()
        if synced != "needs_human":
            return None
        return self._stuck("conflicts with the base branch still unresolved after the resolve sessions")

    async def _ci_green(self, prs: list[PRRef], review: Review) -> RunResult:
        """Wait for CI with fix sessions, asking a person each time it stays red; classify, then merge or wait."""
        if (stuck := await self._ci_loop(prs)) is not None:
            return stuck
        classification = await self.classify(self.prs, review)
        return await self._until_closed(self.prs, review, classification)

    async def _ci_loop(self, prs: list[PRRef]) -> RunResult | None:
        """Loop CI with fix sessions (a conflicting PR takes the base); None once green, else the run's end."""
        syncs = 0
        while True:
            ci = await self.until_ci_green(prs, fix_prompt="fix_ci", max_attempts=MAX_CI_FIX_ATTEMPTS)
            prs = self.prs
            if ci == "passed":
                return None
            if ci == "conflict" and syncs < MAX_BASE_SYNCS:
                syncs += 1
                if (stuck := await self._take_base()) is not None:
                    return stuck
                prs = self.prs
                continue
            urls = ", ".join(pr.url for pr in prs)
            if ci == "conflict":
                return self._stuck(f"still conflicting with the base branch after {syncs} merges: {urls}")
            return self._stuck(f"CI not green after up to {MAX_CI_FIX_ATTEMPTS} fix sessions: {urls}")

    async def _take_base(self) -> RunResult | None:
        """Merge the base branch (conflicts resolved, then the checks looped) and push it; None once pushed."""
        if (stuck := await self._synced()) is not None:
            return stuck
        await self.push_change_set()
        return None

    async def _until_closed(self, prs: list[PRRef], review: Review, classification: Classification) -> RunResult:
        """
        CI is green: merge a change set routed `auto_merge`, else relabel it, tell a person and wait for the PRs.

        A merge GitHub refuses as out of date takes the base (`_take_base`) and CI again, at most `MAX_BASE_SYNCS`
        times, and classifies again if that moved a PR's head; any other refusal goes to a person.
        """
        syncs = 0
        if classification.route == "auto_merge" and not self.input.auto_merge:
            classification = await self.route_to_review(prs, classification, AUTO_MERGE_OFF)
        while classification.route == "auto_merge":
            merged = await self.merge_change_set(prs, self.input.merge_method)
            if merged == "merged":
                return RunResult(outcome=RunOutcome.MERGED, prs=prs, summary=", ".join(pr.url for pr in prs))
            if merged != "out_of_date" or syncs == MAX_BASE_SYNCS:
                reason = MERGE_REFUSED if merged == "refused" else f"still out of date after {syncs} base merges"
                classification = await self.route_to_review(prs, classification, reason)
                break
            syncs += 1
            if (stuck := await self._take_base()) is not None:
                return stuck
            if (stuck := await self._ci_loop(self.prs)) is not None:
                return stuck
            if _heads(self.prs) != _heads(prs):  # the merge, a resolution or a CI fix changed what would merge
                classification = await self.classify(self.prs, review)
            prs = self.prs
        await self.notify_prs_ready(prs, reasons=classification.reasons)
        return await self.until_reviewed(prs, self._on_review)

    async def _on_review(self, event: ReviewEvent) -> RunResult | None:
        """Merge an approved PR alone once CI is green on its head; a review requesting changes gets a round."""
        if event.kind != "approved":
            return await self._review_round(event)
        pushed = _heads(self.prs)
        if (stuck := await self._ci_loop([event.pr])) is not None or _heads(self.prs) != pushed:
            return stuck  # or a CI fix moved the head: the approval was for another commit
        merged = await self.merge_change_set([event.pr], self.input.merge_method)
        if merged == "out_of_date":  # take the base and CI; the new head needs a new approval
            return await self._take_base() or await self._ci_loop(self.prs)
        if merged == "refused":
            await self.notify_merge_refused(event)
        return None  # merged: the next wait sees it

    async def _review_round(self, event: ReviewEvent) -> RunResult | None:
        """Reply to a review requesting changes, rework the change if the session says so, and ask for a review."""
        reply = await self.review_reply(event)
        if reply.answer is None:
            return _ended(REVIEW_REPLY_STAGE, reply.step, self.prs)
        if reply.decision == "implement":
            done = await self._implemented(rework_context(event, reply))
            if isinstance(done, RunResult):
                return done
            await self._learn()
            await self.push_change_set(criteria=done[1].criteria)
            if (stuck := await self._ci_loop(self.prs)) is not None:
                return stuck
        await self.request_review(event.pr, [event.reviewer])
        return None

    def _stuck(self, summary: str) -> RunResult:
        """End the run `needs_human` on a loop out of attempts; the run-end hold says why on the task."""
        return RunResult(outcome=RunOutcome.NEEDS_HUMAN, prs=self.prs, summary=limit_text(summary, SUMMARY_BYTES))

    async def _address(self, review: Review) -> RunResult | None:
        """One round in v0: if the review asks for changes, address them and loop the checks; None when all is well."""
        if not review.needs_changes:
            return None
        addressed = await self.agent("address_review", "address_review", review_context(review), pr_text=False)
        if addressed.outcome != StepOutcome.PASSED:
            return _ended("address_review", addressed, self.prs)
        return await self._checks_green()

    async def _learn(self) -> None:
        """Update the context and write the PR text; a session that does not pass leaves the earlier text in place."""
        try:
            learned = await self.agent("learn", "learn")
        except ActivityError as exc:  # timed out, interrupted twice, or the step could not start
            workflow.logger.warning("learn failed: %s; opening the PR with the earlier text", exc.cause or exc)
            return
        if learned.outcome != StepOutcome.PASSED:
            workflow.logger.warning("learn ended %s; opening the PR with the earlier text", learned.outcome)
