"""`prepare_workspace`: create the run's workspace on a runner, reported as a `shell` step in stage `prepare`."""

from fact.core.activity_contract import PREPARE_WORKSPACE
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
            for name, path in handle.repos.items():
                step.log(f"{name} -> {path}")
            step.finish(StepOutcome.PASSED, outputs={"root": handle.root})
        return handle
