"""
Standard: from a task to a PR.

When the run's `require_plan_approval` is on, an `approve_plan` gate comes first, before any workspace exists: it
shows the task's title, path and acceptance criteria; approve goes on, reject ends the run `stopped` with the note.
Prepare the workspace, run each repository's setup commands, implement, loop lint and test with fix sessions until
green, then review once: a separate session checks the change against the task's acceptance criteria and the review
checklist. An unmet criterion or a medium or high finding sends the change to an `address_review` session, then
through the check loop again. Then a `learn` session updates the repositories' context (`CLAUDE.md`, `docs/`,
READMEs) on the same branch and writes the PR title and summary; mark the task done on the branch and open the PR
(with the review's criteria table). Then wait for CI on the PR's head commit: a failure goes to a `fix_ci` session
with the failing jobs' logs, which commits a fix that is pushed to the PR, then CI again, at most
`MAX_CI_FIX_ATTEMPTS` times. The base branch is merged into the run's branch only when GitHub says it must be, never
because it moved on: when a PR conflicts with its base (`mergeable_state` `dirty`, read by `wait_for_ci` after every
push; GitHub runs no CI on it), and when GitHub refuses the merge as out of date. Both take the same path: merge the
base (`sync_base`, a merge commit, as the PR is squashed), hand conflicts to a `resolve_conflicts` session that commits
the merge, loop the checks (with their gate), push and wait for CI again, at most `MAX_BASE_SYNCS` times each before a
person is asked. Once CI is green, while `MERGE_WHEN_GREEN` is on, fact merges the PRs itself (squash, at the head
commit CI passed on) and the run ends `merged`. If the switch is off or GitHub refuses the merge otherwise, a person is told the PRs are ready, and the run waits (stage `wait_for_review`, a gate-like step counted as
human wait time) until every PR is merged or closed: it ends `merged` if all were merged, else `closed`, and its
workspace is torn down. However the run ends, `FactWorkflow` comments its cost on the PRs last, once every step is
recorded.

A failing setup ends the run `failed`. If the checks never go green, conflicts stay unresolved, or CI stays red or
never finishes, a `needs_human` gate opens: approve runs that loop once more (instructions sent with `steer` reach
its fix sessions), reject ends the run `needs_human`, with no PR for the checks and with the PR open for CI. An agent reporting itself blocked ends the run
`needs_human` with no PR. An agent session that errors, including a review without a valid structured answer, ends the
run `failed`. A `learn` session that does not pass, or whose activity fails, is the exception: its docs are not worth
losing the change over, so the PR still opens with the implement session's title and summary (else the task's
conventional title).

A person is notified when a gate opens, when the PRs are ready for review (CI green, not merged by fact), and when the
run ends `failed` or `needs_human` (`run_failed`) or `merged`, `closed` or `stopped` (`run_finished`).
"""

from temporalio import workflow
from temporalio.exceptions import ActivityError, FailureError

with workflow.unsafe.imports_passed_through():
    from fact.sdk.base_sync import RESOLVE_STAGE, SYNC_STAGE
    from fact.sdk.pull_requests import REVIEW_STAGE
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
APPROVE_PLAN = "approve_plan"  # the plan approval gate's id, kind and stage
# Interim, until the classify stage and auto-merge exist (issues #13, #14): merge every change set whose CI is green,
# with no person, so fact can work through its own tasks unattended. #14 replaces this switch with classification.
MERGE_WHEN_GREEN = True
MAX_BASE_SYNCS = 2  # merges of the base per loop (a conflicting PR, a merge refused as out of date) before a person


def _ended(stage: str, step: StepResult) -> RunResult:
    """End the run after an agent step that did not pass: `needs_human` if the agent said it was blocked."""
    outcome = RunOutcome.NEEDS_HUMAN if step.outcome == StepOutcome.NEEDS_HUMAN else RunOutcome.FAILED
    return RunResult(
        outcome=outcome, summary=limit_text(f"{stage} ended {step.outcome}: {step.summary}", SUMMARY_BYTES)
    )


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
        Stage(SYNC_STAGE, "Merge base branch", StepKind.SHELL),
        Stage(RESOLVE_STAGE, "Resolve conflicts", StepKind.AGENT, loops_to=SYNC_STAGE),
        Stage(REVIEW_STAGE, "Wait for review", StepKind.GATE),
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
        if self.input.require_plan_approval and (stopped := await self._approve_plan()) is not None:
            return stopped
        return await self._to_pr()

    async def _to_pr(self) -> RunResult:
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
        return await self._ship(implemented, review)

    async def _ship(self, implemented: StepResult, review: Review) -> RunResult:
        """Update the context of the reviewed change, open the PR and see it through CI."""
        await self._learn()
        await self.mark_task_done()
        prs = await self.open_change_set(*self.pr_text(implemented.summary), criteria=review.criteria)
        return await self._ci_green(prs)

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
        """Loop the checks with fix sessions, and ask a person each time they stay red; None once they are green."""
        while True:
            checks = await self.until_green(CHECKS, fix_prompt="fix_checks", max_attempts=MAX_FIX_ATTEMPTS)
            if checks == StepOutcome.PASSED:
                return None
            failed = ", ".join(c.name for c in self.check_results if c.outcome != StepOutcome.PASSED)
            summary = f"checks still failing after {MAX_FIX_ATTEMPTS} fix sessions: {failed}"
            if (rejected := await self._ask("checks", summary)) is not None:
                return rejected

    async def _synced(self) -> RunResult | None:
        """Merge the base branch; once an agent resolved conflicts, loop the checks again. None when all is well."""
        while True:
            synced = await self.sync_with_base(RESOLVE_STAGE)
            if synced == "resolved":
                return await self._checks_green()
            if synced != "needs_human":
                return None
            summary = "conflicts with the base branch still unresolved after the resolve sessions"
            if (stuck := await self._ask(SYNC_STAGE, summary)) is not None:
                return stuck

    async def _ci_green(self, prs: list[PRRef]) -> RunResult:
        """Wait for CI with fix sessions, asking a person each time it stays red; then merge or wait, and end."""
        if (stuck := await self._ci_loop(prs)) is not None:
            return stuck
        return await self._until_closed(self.prs)

    async def _ci_loop(self, prs: list[PRRef]) -> RunResult | None:
        """
        Loop CI with fix sessions, asking a person when it stays red; None once green.

        A PR that conflicts with its base takes the base (`_take_base`) and CI again, at most `MAX_BASE_SYNCS` times
        before a person is asked; approving gives it as many more.
        """
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
                loop, summary = SYNC_STAGE, f"still conflicting with the base branch after {syncs} merges: {urls}"
            else:
                loop, summary = "wait_for_ci", f"CI not green after up to {MAX_CI_FIX_ATTEMPTS} fix sessions: {urls}"
            if (stuck := await self._ask(loop, summary)) is not None:
                return stuck
            syncs = 0

    async def _take_base(self) -> RunResult | None:
        """Merge the base branch (conflicts resolved, then the checks looped) and push it; None once pushed."""
        if (stuck := await self._synced()) is not None:
            return stuck
        await self.push_change_set()
        return None

    async def _until_closed(self, prs: list[PRRef]) -> RunResult:
        """
        CI is green: merge (`MERGE_WHEN_GREEN`), else tell a person and wait until every PR is merged or closed.

        A merge GitHub refuses as out of date takes the base (`_take_base`) and CI again, at most `MAX_BASE_SYNCS`
        times; any other refusal goes to a person.
        """
        syncs = 0
        while MERGE_WHEN_GREEN:
            merged = await self.merge_change_set(prs)
            if merged == "merged":
                return RunResult(outcome=RunOutcome.MERGED, prs=prs, summary=", ".join(pr.url for pr in prs))
            if merged != "out_of_date" or syncs == MAX_BASE_SYNCS:
                break
            syncs += 1
            if (stuck := await self._take_base()) is not None:
                return stuck
            if (stuck := await self._ci_loop(self.prs)) is not None:
                return stuck
            prs = self.prs
        await self.notify_prs_ready(prs)
        outcome = await self.wait_for_pr_closed(prs)
        return RunResult(outcome=outcome, prs=prs, summary=", ".join(pr.url for pr in prs))

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
