"""
Standard: from a task to a PR.

Prepare the workspace, run each repository's setup commands, implement, loop lint and test with fix sessions until
green, then review once: a separate session checks the change against the task's acceptance criteria and the review
checklist. An unmet criterion or a medium or high finding sends the change to an `address_review` session, then
through the check loop again. Then mark the task done on the branch, open the PR (with the review's criteria table)
and comment the run's cost on it.

A failing setup ends the run `failed`. If the checks never go green, or an agent reports itself blocked, the run ends
`needs_human` with no PR (M2 turns that into a gate). An agent session that errors, including a review without a valid
structured answer, ends the run `failed`.
"""

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from fact.sdk.review import Review, parse_review, review_context
    from fact.sdk.types import Check, RunInput, RunOutcome, RunResult, StepKind, StepOutcome, StepResult, limit_text
    from fact.sdk.workflow import FactWorkflow, Stage, stages

CHECKS: list[Check] = ["lint", "test"]
MAX_FIX_ATTEMPTS = 3
SUMMARY_BYTES = 1024


def _ended(stage: str, step: StepResult) -> RunResult:
    """End the run after an agent step that did not pass: `needs_human` if the agent said it was blocked."""
    outcome = RunOutcome.NEEDS_HUMAN if step.outcome == StepOutcome.NEEDS_HUMAN else RunOutcome.FAILED
    return RunResult(
        outcome=outcome, summary=limit_text(f"{stage} ended {step.outcome}: {step.summary}", SUMMARY_BYTES)
    )


@stages(
    [
        Stage("prepare", "Prepare workspace", StepKind.SHELL),
        Stage("setup", "Set up workspace", StepKind.SHELL),
        Stage("implement", "Implement", StepKind.AGENT),
        Stage("checks", "Lint and test", StepKind.SHELL, loops_to="implement"),
        Stage("review", "Review", StepKind.AGENT),
        Stage("address_review", "Address review", StepKind.AGENT, loops_to="checks"),
        Stage("open_pr", "Open PR", StepKind.GITHUB),
    ]
)
@workflow.defn(name="standard")
class Standard(FactWorkflow):
    @workflow.run
    async def run(self, run_input: RunInput) -> RunResult:
        return await self.execute(run_input, self._steps)

    async def _steps(self) -> RunResult:
        await self.prepare()
        failed_setup = await self.setup()
        if failed_setup is not None:
            summary = f"setup ended {failed_setup.outcome}: {failed_setup.summary}"
            return RunResult(outcome=RunOutcome.FAILED, summary=limit_text(summary, SUMMARY_BYTES))
        implemented = await self.agent("implement", "implement")
        if implemented.outcome != StepOutcome.PASSED:
            return _ended("implement", implemented)
        if (stuck := await self._checks_green()) is not None:
            return stuck
        reviewed, review = parse_review(await self.agent("review", "review", pr_text=False))
        if review is None:
            return _ended("review", reviewed)
        if (stuck := await self._address(review)) is not None:
            return stuck
        await self.mark_task_done()
        prs = await self.open_change_set(*self.pr_text(implemented.summary), criteria=review.criteria)
        await self.post_cost_comment(prs)
        return RunResult(outcome=RunOutcome.PR_OPENED, prs=prs, summary=", ".join(pr.url for pr in prs))

    async def _checks_green(self) -> RunResult | None:
        """Loop the checks with fix sessions; return the run's end if they never go green, else None."""
        checks = await self.until_green(CHECKS, fix_prompt="fix_checks", max_attempts=MAX_FIX_ATTEMPTS)
        if checks == StepOutcome.PASSED:
            return None
        failed = ", ".join(c.name for c in self.check_results if c.outcome != StepOutcome.PASSED)
        summary = f"checks still failing after {MAX_FIX_ATTEMPTS} fix sessions: {failed}"
        return RunResult(outcome=RunOutcome.NEEDS_HUMAN, summary=limit_text(summary, SUMMARY_BYTES))

    async def _address(self, review: Review) -> RunResult | None:
        """One round in v0: if the review asks for changes, address them and loop the checks; None when all is well."""
        if not review.needs_changes:
            return None
        addressed = await self.agent("address_review", "address_review", review_context(review), pr_text=False)
        if addressed.outcome != StepOutcome.PASSED:
            return _ended("address_review", addressed)
        return await self._checks_green()
