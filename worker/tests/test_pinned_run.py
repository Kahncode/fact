"""A run on a versioned project worker is pinned and records its version: needs the `temporal` CLI (dev server)."""

import shutil
import uuid
from pathlib import Path

import pytest
from fact.core.activity_contract import RECORD_RUN_FINISHED, RECORD_RUN_STARTED, RunFinishedInput, RunStartedInput
from fact.core.enums import RunOutcome
from fact.core.types import RunInput, RunResult, Task
from fact.worker.deployments import deployment_config, set_current_version
from fact.worker.loader import ProjectWorkflows, load_workflows, sandbox_runner, version_id
from fact.worker.ping import CONTROL_QUEUE
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


@pytest.mark.skipif(TEMPORAL is None, reason="the temporal CLI is not on PATH")
async def test_run_is_pinned_and_records_its_version(tmp_path: Path) -> None:
    project, home_sha = f"pin-{uuid.uuid4().hex[:8]}", "c" * 40
    workflows_dir = tmp_path / ".fact" / "workflows"
    workflows_dir.mkdir(parents=True)
    (workflows_dir / "pinned.py").write_text(PINNED, encoding="utf-8")
    workflows = load_workflows(project, tmp_path, home_sha)
    loaded = ProjectWorkflows(project, "acme/home", home_sha, tmp_path, version_id("d" * 40, home_sha), workflows)
    started: list[RunStartedInput] = []

    @activity.defn(name=RECORD_RUN_STARTED)
    async def record_started(spec: RunStartedInput) -> None:
        started.append(spec)

    @activity.defn(name=RECORD_RUN_FINISHED)
    async def record_finished(spec: RunFinishedInput) -> None:
        pass

    env_cm = await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter, dev_server_existing_path=TEMPORAL
    )
    async with (
        env_cm as env,
        Worker(env.client, task_queue=CONTROL_QUEUE, activities=[record_started, record_finished]),
        Worker(
            env.client,
            task_queue=loaded.task_queue,
            workflows=list(workflows),
            workflow_runner=sandbox_runner(),
            deployment_config=deployment_config(loaded),
        ),
    ):
        await set_current_version(env.client, loaded)
        run_input = RunInput(run_id="run-1", project=project, task=Task(key="task:X-1", id="X-1", title="t"))
        handle = await env.client.start_workflow(
            "pinned", run_input, id=f"pinned-{project}", task_queue=loaded.task_queue, result_type=RunResult
        )
        result = await handle.result()
        description = await handle.describe()

    assert result.outcome == RunOutcome.PR_OPENED
    assert description.status == WorkflowExecutionStatus.COMPLETED
    assert [s.worker_version for s in started] == [loaded.version]
    behavior = description.raw_description.workflow_execution_info.versioning_info.behavior
    assert behavior == VersioningBehavior.PINNED.value
