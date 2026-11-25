"""
`prepare_workspace`: create the run's workspace on a runner, reported as a `shell` step in stage `prepare`.

`teardown_workspace`: remove it once the run's work is pushed; bookkeeping, not reported as a step.
"""

from fact.core.activity_contract import PREPARE_WORKSPACE, TEARDOWN_WORKSPACE
from fact.core.enums import StepKind, StepOutcome
from fact.core.types import WorkspaceHandle, WorkspaceSpec
from fact.worker.reporting import StepReporter
from fact.worker.runners import Runner
from temporalio import activity

PREPARE_STAGE = "prepare"


class WorkspaceActivities:
    def __init__(self, runner: Runner, reporter: StepReporter) -> None:
        self._runner = runner
        self._reporter = reporter

    @activity.defn(name=PREPARE_WORKSPACE)
    async def prepare_workspace(self, spec: WorkspaceSpec) -> WorkspaceHandle:
        attempt = activity.info().attempt if activity.in_activity() else 1
        async with self._reporter.step(spec.run_id, PREPARE_STAGE, StepKind.SHELL, attempt) as step:
            step.log(f"preparing workspace for {spec.task_key}: {', '.join(r.full_name for r in spec.repos)}")
            handle = await self._runner.prepare(spec)
            for repo in spec.repos:
                branch = handle.branches.get(repo.full_name, repo.branch)
                taken = f" ({repo.branch} is checked out by another workspace)" if branch != repo.branch else ""
                step.log(f"{repo.full_name} -> {handle.repos[repo.full_name]} on {branch}{taken}")
            step.finish(StepOutcome.PASSED, outputs={"root": handle.root, "branches": handle.branches})
        return handle

    @activity.defn(name=TEARDOWN_WORKSPACE)
    async def teardown_workspace(self, handle: WorkspaceHandle) -> None:
        await self._runner.teardown(handle)
