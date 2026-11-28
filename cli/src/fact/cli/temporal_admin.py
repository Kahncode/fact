"""
What the CLI changes in Temporal itself: deleting drained worker deployment versions, and the namespace retention.

`fact dev` sets the namespace's retention (`temporal.retention_days`) once Temporal accepts connections, so Temporal
deletes closed workflow histories itself; `fact cleanup` deletes the versions of the generations it removes.
"""

import asyncio
import time
from collections.abc import Callable, Sequence
from datetime import timedelta

from fact.cli.drainage import TIMEOUT_S, Version
from fact.cli.supervisor import Changes, port_open
from fact.core.settings import Settings
from google.protobuf.duration_pb2 import Duration
from temporalio.api.deployment.v1 import WorkerDeploymentVersion
from temporalio.api.namespace.v1 import NamespaceConfig
from temporalio.api.workflowservice.v1 import (
    DeleteWorkerDeploymentVersionRequest,
    DescribeNamespaceRequest,
    UpdateNamespaceRequest,
)
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

IDENTITY = "fact-cli"
RETRY_S = 5.0  # between two tries at setting the retention


async def _connect(settings: Settings) -> Client:
    return await Client.connect(settings.temporal.address, namespace=settings.temporal.namespace)


async def _delete(client: Client, version: Version) -> str | None:
    deployment, build_id = version
    request = DeleteWorkerDeploymentVersionRequest(
        namespace=client.namespace,
        deployment_version=WorkerDeploymentVersion(deployment_name=deployment, build_id=build_id),
        identity=IDENTITY,
    )
    try:
        await client.workflow_service.delete_worker_deployment_version(request)
    except RPCError as exc:
        return None if exc.status == RPCStatusCode.NOT_FOUND else exc.message or str(exc)
    return None


def delete_versions(settings: Settings, versions: Sequence[Version]) -> dict[Version, str]:
    """Delete each version from Temporal; return why for each one it refused (or every one, when it is down)."""
    if not versions:
        return {}

    async def run() -> dict[Version, str]:
        async with asyncio.timeout(TIMEOUT_S * 3):
            client = await _connect(settings)
            return {v: e for v in versions if (e := await _delete(client, v)) is not None}

    try:
        return asyncio.run(run())
    except (RPCError, RuntimeError, OSError, TimeoutError) as exc:
        return dict.fromkeys(versions, f"Temporal cannot be reached ({exc})")


async def set_retention(settings: Settings) -> None:
    """Make the namespace keep closed workflow histories `temporal.retention_days`."""
    client = await _connect(settings)
    ttl = Duration()
    ttl.FromTimedelta(timedelta(days=settings.temporal.retention_days))
    request = UpdateNamespaceRequest(
        namespace=client.namespace, config=NamespaceConfig(workflow_execution_retention_ttl=ttl)
    )
    await client.workflow_service.update_namespace(request)


async def retention(settings: Settings) -> timedelta:
    """Return the namespace's retention, as Temporal reports it."""
    client = await _connect(settings)
    response = await client.workflow_service.describe_namespace(DescribeNamespaceRequest(namespace=client.namespace))
    return response.config.workflow_execution_retention_ttl.ToTimedelta()


class NamespaceRetention:
    """A `fact dev` restarter that sets the namespace retention once Temporal is up, then does nothing."""

    def __init__(
        self,
        settings: Settings,
        endpoint: tuple[str, int],
        log: Callable[[str], None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings, self._endpoint, self._log, self._clock = settings, endpoint, log, clock
        self.done = False
        self._next = 0.0

    def __call__(self) -> Changes | None:
        if self.done or self._clock() < self._next:
            return None
        self._next = self._clock() + RETRY_S
        if not port_open(*self._endpoint):
            return None
        try:
            asyncio.run(asyncio.wait_for(set_retention(self._settings), TIMEOUT_S))
        except (RPCError, RuntimeError, OSError, TimeoutError) as exc:
            self._log(f"cannot set the Temporal namespace retention yet: {exc}")
            return None
        self.done = True
        days = self._settings.temporal.retention_days
        self._log(
            f"Temporal keeps closed workflow histories {days} days (namespace {self._settings.temporal.namespace})"
        )
        return None
