"""
Which worker deployment versions still need a worker, as Temporal reports them.

Needs the `temporal` CLI (dev server): the time-skipping test server has no Worker Deployment Versioning.
"""

import asyncio
import shutil
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack

import pytest
from fact.cli import drainage
from fact.core.settings import Settings
from temporalio import workflow
from temporalio.api.workflowservice.v1 import SetWorkerDeploymentCurrentVersionRequest
from temporalio.client import Client, WorkflowHandle
from temporalio.common import VersioningBehavior, WorkerDeploymentVersion
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker, WorkerDeploymentConfig

TEMPORAL = shutil.which("temporal")
# drainage is checked every few minutes by default; the test cannot wait that long
FAST_DRAINAGE = [
    "--dynamic-config-value",
    'matching.wv.VersionDrainageStatusRefreshInterval="1s"',
    "--dynamic-config-value",
    'matching.wv.VersionDrainageStatusVisibilityGracePeriod="1s"',
]


@workflow.defn(name="waiter")
class Waiter:
    def __init__(self) -> None:
        self.done = False

    @workflow.signal
    def finish(self) -> None:
        self.done = True

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self.done)


def _worker(client: Client, queue: str, build_id: str) -> Worker:
    version = WorkerDeploymentVersion(deployment_name=queue, build_id=build_id)
    config = WorkerDeploymentConfig(
        version=version, use_worker_versioning=True, default_versioning_behavior=VersioningBehavior.PINNED
    )
    runner = UnsandboxedWorkflowRunner()  # the sandbox cannot re-import a test module
    return Worker(client, task_queue=queue, workflows=[Waiter], workflow_runner=runner, deployment_config=config)


async def _make_current(client: Client, queue: str, build_id: str) -> None:
    request = SetWorkerDeploymentCurrentVersionRequest(
        namespace=client.namespace, deployment_name=queue, build_id=build_id, allow_no_pollers=True
    )
    for _ in range(50):  # the server registers a polling version shortly after the first poll
        try:
            await client.workflow_service.set_worker_deployment_current_version(request)
        except Exception:  # noqa: BLE001
            await asyncio.sleep(0.2)
        else:
            return
    pytest.fail(f"{queue}@{build_id} never became current")


async def _until(predicate: Callable[[], Awaitable[bool]]) -> None:
    """Poll the server until `predicate` holds, for at most 30 s."""
    async with asyncio.timeout(30):
        while not await predicate():  # noqa: ASYNC110 - the server's state, not an event of ours
            await asyncio.sleep(0.5)


@pytest.mark.skipif(TEMPORAL is None, reason="the temporal CLI is not on PATH")
async def test_an_old_version_needs_a_worker_until_its_last_run_ends() -> None:
    queue = f"wf-drain-{uuid.uuid4().hex[:8]}"
    old, new = (queue, "old"), (queue, "new")
    env_cm = await WorkflowEnvironment.start_local(
        dev_server_existing_path=TEMPORAL, dev_server_extra_args=FAST_DRAINAGE
    )
    async with env_cm as env, AsyncExitStack() as stack:
        settings = Settings(temporal={"address": env.client.service_client.config.target_host})
        await stack.enter_async_context(_worker(env.client, queue, "old"))
        await _make_current(env.client, queue, "old")
        handle = await env.client.start_workflow("waiter", id=f"run-{queue}", task_queue=queue)
        await _until(lambda: _pinned(handle))
        await stack.enter_async_context(_worker(env.client, queue, "new"))
        await _make_current(env.client, queue, "new")

        assert await drainage._in_use(settings, [old, new, (queue, "never")]) == {old, new}  # noqa: SLF001

        await handle.signal("finish")
        await handle.result()

        async def drained() -> bool:
            return await drainage._in_use(settings, [old, new]) == {new}  # noqa: SLF001

        await _until(drained)


async def _pinned(handle: WorkflowHandle) -> bool:
    description = await handle.describe()
    info = description.raw_description.workflow_execution_info.versioning_info
    return info.behavior == VersioningBehavior.PINNED.value


def test_temporal_down_is_unknown_not_drained() -> None:
    assert drainage.versions_in_use(Settings(temporal={"address": "localhost:1"}), [("wf-x", "b")]) is None
