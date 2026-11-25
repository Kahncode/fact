"""
Standard: from a task to a PR.

Prepare the workspace, implement, loop lint and test with fix sessions until green, mark the task done on the branch,
open the PR and comment the run's cost on it. If the checks never go green, or the implement agent reports itself
blocked, the run ends `needs_human` with no PR (M2 turns that into a gate).
"""

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from fact.sdk.types import Check, RunInput, RunOutcome, RunResult, StepKind, StepOutcome, limit_text
    from fact.sdk.workflow import FactWorkflow, Stage, stages

CHECKS: list[Check] = ["lint", "test"]
MAX_FIX_ATTEMPTS = 3
SUMMARY_BYTES = 1024


@stages(
    [
        Stage("prepare", "Prepare workspace", StepKind.SHELL),
        Stage("implement", "Implement", StepKind.AGENT),
        Stage("checks", "Lint and test", StepKind.SHELL, loops_to="implement"),
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
        implemented = await self.agent("implement", "implement")
        if implemented.outcome != StepOutcome.PASSED:
            summary = f"implement ended {implemented.outcome}: {implemented.summary}"
            blocked = implemented.outcome == StepOutcome.NEEDS_HUMAN  # the agent said it could not finish
            outcome = RunOutcome.NEEDS_HUMAN if blocked else RunOutcome.FAILED
            return RunResult(outcome=outcome, summary=limit_text(summary, SUMMARY_BYTES))
        checks = await self.until_green(CHECKS, fix_prompt="fix_checks", max_attempts=MAX_FIX_ATTEMPTS)
        if checks != StepOutcome.PASSED:
            failed = ", ".join(c.name for c in self.check_results if c.outcome != StepOutcome.PASSED)
            summary = f"checks still failing after {MAX_FIX_ATTEMPTS} fix sessions: {failed}"
            return RunResult(outcome=RunOutcome.NEEDS_HUMAN, summary=limit_text(summary, SUMMARY_BYTES))
        await self.mark_task_done()
        prs = await self.open_change_set(*self.pr_text(implemented.summary))
        await self.post_cost_comment(prs)
        return RunResult(outcome=RunOutcome.PR_OPENED, prs=prs, summary=", ".join(pr.url for pr in prs))
