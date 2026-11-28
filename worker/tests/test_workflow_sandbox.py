"""A workflow's sandbox and `docker run` arguments reach `prepare_workspace`, which fills in its version's default."""

import uuid
from pathlib import Path

import pytest
from fact.core.activity_contract import CONTROL_QUEUE, PREPARE_WORKSPACE
from fact.core.types import RunInput, Task, WorkspaceHandle, WorkspaceSpec
from fact.sdk.workflow import FactWorkflow
from fact.worker.activities.workspace import WorkspaceActivities
from fact.worker.reporting import StepReporter
from httpx import AsyncClient
from temporal_env import time_skipping_env
from temporalio import activity, workflow
from temporalio.testing import ActivityEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

QUEUE = "wf-sandbox-test"


@workflow.defn(name="named_sandbox")
class NamedSandbox(FactWorkflow):
    sandbox = "python"
    sandbox_args = ("--shm-size", "1g")

    @workflow.run
    async def run(self, run_input: RunInput) -> None:
        self._input = run_input
        await self.prepare()


@workflow.defn(name="default_sandbox")
class DefaultSandbox(FactWorkflow):
    @workflow.run
    async def run(self, run_input: RunInput) -> None:
        self._input = run_input
        await self.prepare()


@pytest.mark.parametrize(
    ("workflow_name", "sandbox", "args"),
    [("named_sandbox", "python", ["--shm-size", "1g"]), ("default_sandbox", None, [])],
)
async def test_workspace_spec_carries_the_sandbox(workflow_name: str, sandbox: str | None, args: list[str]) -> None:
    specs: list[WorkspaceSpec] = []

    @activity.defn(name=PREPARE_WORKSPACE)
    async def prepare(spec: WorkspaceSpec) -> WorkspaceHandle:
        specs.append(spec)
        return WorkspaceHandle(run_id=spec.run_id, root="/ws", task_queue="sandbox")

    env = await time_skipping_env()
    task = Task(key="task:X-1", id="X-1", title="t")
    run_input = RunInput(run_id=f"run-{uuid.uuid4().hex[:8]}", project="acme", task=task)
    async with (
        Worker(env.client, task_queue=CONTROL_QUEUE, activities=[prepare]),
        Worker(
            env.client,
            task_queue=QUEUE,
            workflows=[NamedSandbox, DefaultSandbox],
            workflow_runner=UnsandboxedWorkflowRunner(),  # defined here, not in a workflow file
        ),
    ):
        await env.client.execute_workflow(workflow_name, run_input, id=run_input.run_id, task_queue=QUEUE)

    assert [(s.sandbox, s.sandbox_args) for s in specs] == [(sandbox, args)]


class _RecordingRunner:
    task_queue = "sandbox"

    def __init__(self) -> None:
        self.specs: list[WorkspaceSpec] = []

    async def prepare(self, spec: WorkspaceSpec) -> WorkspaceHandle:
        self.specs.append(spec)
        return WorkspaceHandle(run_id=spec.run_id, root="/ws", task_queue=self.task_queue)


@pytest.mark.parametrize(("named", "expected"), [(None, "base"), ("python", "python")])
async def test_prepare_workspace_maps_no_sandbox_to_its_versions_default(
    client: AsyncClient, tmp_path: Path, run_id: str, named: str | None, expected: str
) -> None:
    runner = _RecordingRunner()
    reporter = StepReporter("http://test", client, log_dir=tmp_path / "logs")
    activities = WorkspaceActivities(runner, reporter, default_sandbox="base")  # type: ignore[arg-type]
    spec = WorkspaceSpec(run_id=run_id, task_key="task:X-1", repos=[], sandbox=named)

    await ActivityEnvironment().run(activities.prepare_workspace, spec)

    assert [s.sandbox for s in runner.specs] == [expected]
