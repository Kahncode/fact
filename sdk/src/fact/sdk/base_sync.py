"""
The steps of `FactWorkflow` that keep the run's branch up to date with its base branch.

`sync_with_base` merges each repository's default branch into the run's branch (a merge commit: the PR is squashed in
the end) and hands conflicts to an agent session, which resolves them and commits the merge. `BaseSyncSteps` is a
mixin of `FactWorkflow` only: it uses the workflow's `input`, `handle`, `next_attempt`, `note_outcome`, `agent` and
`raise_if_stopped`.
"""

from datetime import timedelta
from typing import TYPE_CHECKING, Literal

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from fact.core.activity_contract import CONTROL_QUEUE, SYNC_BASE, SyncBaseInput
    from fact.core.enums import StepOutcome
    from fact.core.types import StepResult
    from fact.sdk.policies import HEARTBEAT_TIMEOUT, INFRA_RETRY, WAIT_CANCELLED

if TYPE_CHECKING:
    from fact.sdk.workflow import FactWorkflow

SYNC_STAGE = "sync_base"
RESOLVE_STAGE = "resolve_conflicts"
MAX_RESOLVE_ATTEMPTS = 2
SYNC_TIMEOUT = timedelta(minutes=30)  # a fetch and a merge per repository, each git call up to 10 min; it heartbeats
# `up_to_date`: nothing to merge; `merged`: a clean merge commit; `resolved`: an agent resolved conflicts, so the
# change needs its checks again; `needs_human`: the conflicts are still there
BaseSync = Literal["up_to_date", "merged", "resolved", "needs_human"]


def conflict_context(result: StepResult) -> str:
    """Build the text a `resolve_conflicts` session gets: the unmerged paths of each repository."""
    conflicts = result.outputs.get("conflicts")
    if not isinstance(conflicts, dict) or not conflicts:
        return result.summary
    parts = [
        f"### {repo}\n\n" + "\n".join(f"- `{path}`" for path in paths) for repo, paths in sorted(conflicts.items())
    ]
    return "Merging the base branch left these files unmerged:\n\n" + "\n\n".join(parts)


class BaseSyncSteps:
    async def sync_base(self: FactWorkflow, stage_id: str = SYNC_STAGE, *, verify_only: bool = False) -> StepResult:
        """
        Merge the default branch into every repository's branch: `passed`, or `failed_checks` on a conflict.

        `outputs.merged` names the repositories that got a merge commit; `outputs.conflicts` maps each conflicting
        repository to its unmerged paths, whose merge is left in progress. The next call commits a merge whose paths
        are all resolved. `verify_only` (after a resolve session) only checks and concludes that merge: a base that
        moved on meanwhile is GitHub's to report, not this step's to chase.
        """
        spec = SyncBaseInput(
            run_id=self.input.run_id,
            handle=self.handle,
            repos=self.input.repos,
            stage_id=stage_id,
            attempt=self.next_attempt(stage_id),
            verify_only=verify_only,
        )
        self.raise_if_stopped()
        result = await workflow.execute_activity(
            SYNC_BASE,
            spec,
            result_type=StepResult,
            task_queue=CONTROL_QUEUE,
            start_to_close_timeout=SYNC_TIMEOUT,
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
            retry_policy=INFRA_RETRY,
            cancellation_type=WAIT_CANCELLED,  # a stop tears the worktree down only once git has stopped in it
        )
        self.note_outcome(stage_id, spec.attempt, result.outcome)
        return result

    async def sync_with_base(
        self: FactWorkflow,
        fix_prompt: str = RESOLVE_STAGE,
        max_attempts: int = MAX_RESOLVE_ATTEMPTS,
        *,
        stage_id: str = SYNC_STAGE,
        fix_stage: str = RESOLVE_STAGE,
    ) -> BaseSync:
        """
        Merge the base branch; on conflicts, a `fix_stage` session resolves them, then merge again to check.

        `max_attempts` caps the sessions. Returns `up_to_date`, `merged`, `resolved` (run the checks again: an agent
        changed code), or `needs_human` once the sessions are exhausted or one does not pass. Nothing is pushed.
        """
        resolved = False
        for sessions in range(max_attempts + 1):
            result = await self.sync_base(stage_id, verify_only=sessions > 0)
            if result.outcome == StepOutcome.PASSED:
                if resolved:
                    return "resolved"
                return "merged" if result.outputs.get("merged") else "up_to_date"
            if sessions == max_attempts:
                break
            fixed = await self.agent(fix_stage, fix_prompt, context=conflict_context(result), pr_text=False)
            if fixed.outcome != StepOutcome.PASSED:
                break
            resolved = True
        return "needs_human"
