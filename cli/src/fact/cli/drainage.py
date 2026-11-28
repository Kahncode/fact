"""
Ask Temporal which worker deployment versions still need a worker.

A version needs one while it is current or ramping (new runs go to it) or while its drainage status is `DRAINING`
(runs are still pinned to it). One `DRAINED`, inactive or unknown to Temporal has nothing left to serve.
"""

import asyncio
from collections.abc import Sequence

from fact.core.settings import Settings
from temporalio.api.deployment.v1 import WorkerDeploymentVersion
from temporalio.api.enums.v1 import VersionDrainageStatus, WorkerDeploymentVersionStatus
from temporalio.api.workflowservice.v1 import (
    DescribeWorkerDeploymentVersionRequest,
    DescribeWorkerDeploymentVersionResponse,
)
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

TIMEOUT_S = 10.0  # the whole check; the server is local
Version = tuple[str, str]  # (deployment name, build id)
SERVING = (
    WorkerDeploymentVersionStatus.WORKER_DEPLOYMENT_VERSION_STATUS_CURRENT,
    WorkerDeploymentVersionStatus.WORKER_DEPLOYMENT_VERSION_STATUS_RAMPING,
)


def needs_worker(response: DescribeWorkerDeploymentVersionResponse) -> bool:
    info = response.worker_deployment_version_info
    if info.status in SERVING:
        return True
    return info.drainage_info.status == VersionDrainageStatus.VERSION_DRAINAGE_STATUS_DRAINING


async def _needs_worker(client: Client, version: Version) -> bool:
    deployment, build_id = version
    request = DescribeWorkerDeploymentVersionRequest(
        namespace=client.namespace,
        deployment_version=WorkerDeploymentVersion(deployment_name=deployment, build_id=build_id),
    )
    try:
        response = await client.workflow_service.describe_worker_deployment_version(request)
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:  # never polled: nothing is pinned to it
            return False
        raise
    return needs_worker(response)


async def _in_use(settings: Settings, versions: Sequence[Version]) -> set[Version]:
    client = await Client.connect(settings.temporal.address, namespace=settings.temporal.namespace)
    return {v for v in versions if await _needs_worker(client, v)}


def versions_in_use(settings: Settings, versions: Sequence[Version]) -> set[Version] | None:
    """Return the versions that need a worker, or None when Temporal cannot tell (down, or an error)."""
    if not versions:
        return set()

    async def check() -> set[Version]:
        async with asyncio.timeout(TIMEOUT_S):
            return await _in_use(settings, versions)

    try:
        return asyncio.run(check())
    except RPCError, RuntimeError, OSError, TimeoutError:
        return None
