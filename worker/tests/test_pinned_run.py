"""
A run on a versioned project worker is pinned, records its version and runs its activities on workers of that build.

Needs the `temporal` CLI (dev server): the time-skipping test server has no Worker Deployment Versioning.
"""

import shutil
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fact.core.activity_contract import (
    NOTIFY,
    RECORD_RUN_FINISHED,
    RECORD_RUN_STARTED,
    REPORT_STATUS,
    NotifyInput,
    ReportStatusInput,
    RunFinishedInput,
    RunStartedInput,
)
from fact.core.enums import RunOutcome
from fact.core.types import RunInput, RunResult, Task
from fact.worker.activity_workers import activity_workers
from fact.worker.deployments import deployment_config, set_current_version
from fact.worker.loader import ProjectWorkflows, load_workflows, sandbox_runner, version_id
from temporalio import activity
from temporalio.client import WorkflowExecutionStatus
from temporalio.common import VersioningBehavior
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

TEMPORAL = shutil.which("temporal")
PINNED = """
from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from fact.sdk.types import RunInput, RunOutcome, RunResult
    from fact.sdk.workflow import FactWorkflow


@workflow.defn(name="pinned")
class Pinned(FactWorkflow):
    @workflow.run
    async def run(self, run_input: RunInput) -> RunResult:
        return await self.execute(run_input, self._body)

    async def _body(self) -> RunResult:
        return RunResult(outcome=RunOutcome.PR_OPENED, summary="ok")
"""


@dataclass
class Build:
    """The control activities of one factory build, recording which build ran each call."""

    name: str
    ran: list[tuple[str, str]] = field(default_factory=list)  # (activity, build)

    def activities(self) -> list:
        @activity.defn(name=RECORD_RUN_STARTED)
        async def record_started(spec: RunStartedInput) -> None:
            self.ran.append((RECORD_RUN_STARTED, spec.worker_version or ""))

        @activity.defn(name=RECORD_RUN_FINISHED)
        async def record_finished(spec: RunFinishedInput) -> None:  # noqa: ARG001
            self.ran.append((RECORD_RUN_FINISHED, self.name))

        @activity.defn(name=REPORT_STATUS)
        async def report_status(spec: ReportStatusInput) -> None:  # noqa: ARG001
            self.ran.append((REPORT_STATUS, self.name))

        @activity.defn(name=NOTIFY)
        async def notify(spec: NotifyInput) -> int:  # noqa: ARG001 - `pr_opened` holds the task: `task_held`
            self.ran.append((NOTIFY, self.name))
            return 1

        return [record_started, record_finished, report_status, notify]


@pytest.mark.skipif(TEMPORAL is None, reason="the temporal CLI is not on PATH")
async def test_run_is_pinned_and_runs_its_activities_on_its_build(tmp_path: Path) -> None:
    project, home_sha = f"pin-{uuid.uuid4().hex[:8]}", "c" * 40
    workflows_dir = tmp_path / ".fact" / "workflows"
    workflows_dir.mkdir(parents=True)
    (workflows_dir / "pinned.py").write_text(PINNED, encoding="utf-8")
    workflows = load_workflows(project, tmp_path, home_sha)
    current = ProjectWorkflows(project, "acme/home", home_sha, tmp_path, version_id("d" * 40, home_sha), workflows)
    other = ProjectWorkflows(project, "acme/home", home_sha, tmp_path, version_id("e" * 40, home_sha), workflows)
    builds = {current.version: Build(current.version), other.version: Build(other.version)}

    env_cm = await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter, dev_server_existing_path=TEMPORAL
    )
    async with env_cm as env, AsyncExitStack() as stack:
        # both builds poll `control`; only `current` runs the project's workflows
        workers = [
            *activity_workers(env.client, current, builds[current.version].activities(), workspace_queue="ws"),
            *activity_workers(env.client, other, builds[other.version].activities(), workspace_queue="ws"),
            Worker(
                env.client,
                task_queue=current.task_queue,
                workflows=list(workflows),
                workflow_runner=sandbox_runner(),
                deployment_config=deployment_config(current),
            ),
        ]
        for worker in workers:
            await stack.enter_async_context(worker)
        await set_current_version(env.client, current)
        run_input = RunInput(run_id="run-1", project=project, task=Task(key="task:X-1", id="X-1", title="t"))
        handle = await env.client.start_workflow(
            "pinned", run_input, id=f"pinned-{project}", task_queue=current.task_queue, result_type=RunResult
        )
        result = await handle.result()
        description = await handle.describe()

    assert result.outcome == RunOutcome.PR_OPENED
    assert description.status == WorkflowExecutionStatus.COMPLETED
    behavior = description.raw_description.workflow_execution_info.versioning_info.behavior
    assert behavior == VersioningBehavior.PINNED.value
    ran = builds[current.version].ran
    assert ran[0] == (RECORD_RUN_STARTED, current.version)  # the run records the build it is pinned to
    assert {build for _, build in ran} == {current.version}
    assert builds[other.version].ran == []  # never on the other build's workers, though they poll the same queue
