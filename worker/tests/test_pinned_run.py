"""
A run on a versioned project worker is pinned, records its version and runs its activities on workers of that build.

Needs the `temporal` CLI (dev server): the time-skipping test server has no Worker Deployment Versioning.
"""

import asyncio
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
    def __init__(self) -> None:
        super().__init__()
        self.go = False

    @workflow.signal
    def release(self) -> None:
        self.go = True

    @workflow.run
    async def run(self, run_input: RunInput) -> RunResult:
        return await self.execute(run_input, self._body)

    async def _body(self) -> RunResult:
        if self.input.task.title == "wait":
            await workflow.wait_condition(lambda: self.go)
        return RunResult(outcome=RunOutcome.PR_OPENED, summary="ok")
"""


@dataclass
class Build:
    """The control activities of one factory build, recording which build ran each call."""

    name: str
    ran: list[tuple[str, str]] = field(default_factory=list)  # (activity, build)
    any_ran: asyncio.Event = field(default_factory=asyncio.Event)  # set once an activity ran on this build

    def _record(self, name: str, build: str) -> None:
        self.ran.append((name, build))
        self.any_ran.set()

    def activities(self) -> list:
        @activity.defn(name=RECORD_RUN_STARTED)
        async def record_started(spec: RunStartedInput) -> None:
            self._record(RECORD_RUN_STARTED, spec.worker_version or "")

        @activity.defn(name=RECORD_RUN_FINISHED)
        async def record_finished(spec: RunFinishedInput) -> None:  # noqa: ARG001
            self._record(RECORD_RUN_FINISHED, self.name)

        @activity.defn(name=REPORT_STATUS)
        async def report_status(spec: ReportStatusInput) -> None:  # noqa: ARG001
            self._record(REPORT_STATUS, self.name)

        @activity.defn(name=NOTIFY)
        async def notify(spec: NotifyInput) -> int:  # noqa: ARG001 - `pr_opened` holds the task: `task_held`
            self._record(NOTIFY, self.name)
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


def _workflow_worker(client: object, project: ProjectWorkflows) -> Worker:
    return Worker(
        client,  # type: ignore[arg-type]
        task_queue=project.task_queue,
        workflows=list(project.workflows),
        workflow_runner=sandbox_runner(),
        deployment_config=deployment_config(project),
    )


@pytest.mark.skipif(TEMPORAL is None, reason="the temporal CLI is not on PATH")
async def test_two_generations_each_run_their_own_runs_activities(tmp_path: Path) -> None:
    """Generation A's waiting run ends on A once B is current and B's runs start on B, all on one `control` queue."""
    project, home_a, home_b = f"gen-{uuid.uuid4().hex[:8]}", "a" * 40, "b" * 40
    workflows_dir = tmp_path / ".fact" / "workflows"
    workflows_dir.mkdir(parents=True)
    (workflows_dir / "pinned.py").write_text(PINNED, encoding="utf-8")
    gen_a = ProjectWorkflows(
        project, "acme/home", home_a, tmp_path, version_id("d" * 40, home_a), load_workflows(project, tmp_path, home_a)
    )
    gen_b = ProjectWorkflows(
        project, "acme/home", home_b, tmp_path, version_id("e" * 40, home_b), load_workflows(project, tmp_path, home_b)
    )
    builds = {gen_a.version: Build(gen_a.version), gen_b.version: Build(gen_b.version)}

    env_cm = await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter, dev_server_existing_path=TEMPORAL
    )
    async with env_cm as env, AsyncExitStack() as stack:
        for gen in (gen_a, gen_b):
            for worker in [
                *activity_workers(env.client, gen, builds[gen.version].activities(), workspace_queue="ws"),
                _workflow_worker(env.client, gen),
            ]:
                await stack.enter_async_context(worker)

        async def start(run_id: str, title: str) -> object:
            run_input = RunInput(
                run_id=run_id, project=project, task=Task(key=f"task:{run_id}", id=run_id, title=title)
            )
            return await env.client.start_workflow(
                "pinned", run_input, id=f"{run_id}-{project}", task_queue=gen_a.task_queue, result_type=RunResult
            )

        await set_current_version(env.client, gen_a)
        waiting = await start("a-1", "wait")
        async with asyncio.timeout(10):  # pinned once its first workflow task completes on A
            await builds[gen_a.version].any_ran.wait()
        await set_current_version(env.client, gen_b)  # `fact use`: B's worker starts and is current
        newer = await start("b-1", "done")
        assert (await newer.result()).outcome == RunOutcome.PR_OPENED
        await waiting.signal("release")  # the waiting run's PR merges
        assert (await waiting.result()).outcome == RunOutcome.PR_OPENED

    ran_a, ran_b = builds[gen_a.version].ran, builds[gen_b.version].ran
    assert ran_a[0] == (RECORD_RUN_STARTED, gen_a.version)
    assert ran_b[0] == (RECORD_RUN_STARTED, gen_b.version)
    assert {build for _, build in ran_a} == {gen_a.version}  # A's run ended on A, after B became current
    assert {build for _, build in ran_b} == {gen_b.version}
    assert [name for name, _ in ran_a].count(RECORD_RUN_FINISHED) == 1
    assert [name for name, _ in ran_b].count(RECORD_RUN_FINISHED) == 1
