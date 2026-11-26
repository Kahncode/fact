"""
Standard: from a task to a PR.

Prepare the workspace, run each repository's setup commands, implement, loop lint and test with fix sessions until
green, then review once: a separate session checks the change against the task's acceptance criteria and the review
checklist. An unmet criterion or a medium or high finding sends the change to an `address_review` session, then
through the check loop again. Then a `learn` session updates the repositories' context (`CLAUDE.md`, `docs/`,
READMEs) on the same branch and writes the PR title and summary; mark the task done on the branch, open the PR (with
the review's criteria table) and comment the run's cost on it. Then wait for CI on the PR's head commit: a failure goes
to a `fix_ci` session with the failing jobs' logs, which commits a fix that is pushed to the PR, then CI again, at most
`MAX_CI_FIX_ATTEMPTS` times. The cost comment is updated at the end of the run.

A failing setup ends the run `failed`. If the checks never go green, or CI stays red or never finishes, a `needs_human`
gate opens: approve runs that loop once more (instructions sent with `steer` reach its fix sessions), reject ends the
run `needs_human`, with no PR for the checks and with the PR open for CI. An agent reporting itself blocked ends the run
`needs_human` with no PR. An agent session that errors, including a review without a valid structured answer, ends the
run `failed`. A `learn` session that does not pass, or whose activity fails, is the exception: its docs are not worth
losing the change over, so the PR still opens with the implement session's title and summary (else the task's
conventional title).

A person is notified when a gate opens, when the PRs are ready (CI green), and when the run ends `failed` or
`needs_human`.
"""

from temporalio import workflow
from temporalio.exceptions import ActivityError, FailureError

with workflow.unsafe.imports_passed_through():
    from fact.sdk.review import Review, parse_review, review_context
    from fact.sdk.types import (
        Check,
        GateDecision,
        PRRef,
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
NEEDS_HUMAN = "needs_human"  # the gate kind, and its stage


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
        Stage("learn", "Learn", StepKind.AGENT),
        Stage("open_pr", "Open PR", StepKind.GITHUB),
        Stage("wait_for_ci", "Wait for CI", StepKind.GITHUB, loops_to="fix_ci"),
        Stage("fix_ci", "Fix CI", StepKind.AGENT, loops_to="open_pr"),
        Stage(NEEDS_HUMAN, "Needs a person", StepKind.GATE),
    ]
)
@workflow.defn(name="standard")
class Standard(FactWorkflow):
    def __init__(self) -> None:
        super().__init__()
        self._gates = 0  # `needs_human` gates opened so far, for unique gate ids

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
        await self._learn()
        await self.mark_task_done()
        prs = await self.open_change_set(*self.pr_text(implemented.summary), criteria=review.criteria)
        await self.post_cost_comment(prs)
        return await self._ci_green(prs)

    async def _checks_green(self) -> RunResult | None:
        """Loop the checks with fix sessions, and ask a person each time they stay red; None once they are green."""
        while True:
            checks = await self.until_green(CHECKS, fix_prompt="fix_checks", max_attempts=MAX_FIX_ATTEMPTS)
            if checks == StepOutcome.PASSED:
                return None
            failed = ", ".join(c.name for c in self.check_results if c.outcome != StepOutcome.PASSED)
            summary = f"checks still failing after {MAX_FIX_ATTEMPTS} fix sessions: {failed}"
            if (rejected := await self._ask("checks", summary)) is not None:
                return rejected

    async def _ci_green(self, prs: list[PRRef]) -> RunResult:
        """Wait for CI with fix sessions, asking a person each time it stays red; update the cost comment, and end."""
        while True:
            ci = await self.until_ci_green(prs, fix_prompt="fix_ci", max_attempts=MAX_CI_FIX_ATTEMPTS)
            prs = self.prs
            urls = ", ".join(pr.url for pr in prs)
            if ci == StepOutcome.PASSED:
                result: RunResult | None = RunResult(outcome=RunOutcome.PR_OPENED, prs=prs, summary=urls)
                break
            summary = f"CI not green after up to {MAX_CI_FIX_ATTEMPTS} fix sessions: {urls}"
            if (result := await self._ask("wait_for_ci", summary)) is not None:
                break
        await self.post_cost_comment(prs)  # again, with the CI wait, the fixes and any human wait
        return result

    async def _ask(self, loop: str, summary: str) -> RunResult | None:
        """Open a `needs_human` gate on an exhausted loop: None if approved (run it again), else the run's end."""
        self._gates += 1
        answer = await self.gate(f"{loop}-{self._gates}", NEEDS_HUMAN, {"loop": loop, "summary": summary})
        if answer.decision == GateDecision.APPROVE:
            return None
        text = f"{summary}; rejected: {answer.note}" if answer.note else summary
        return RunResult(outcome=RunOutcome.NEEDS_HUMAN, prs=self.prs, summary=limit_text(text, SUMMARY_BYTES))

    async def _address(self, review: Review) -> RunResult | None:
        """One round in v0: if the review asks for changes, address them and loop the checks; None when all is well."""
        if not review.needs_changes:
            return None
        addressed = await self.agent("address_review", "address_review", review_context(review), pr_text=False)
        if addressed.outcome != StepOutcome.PASSED:
            return _ended("address_review", addressed)
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
