"""
Worker Deployment Versioning for project workflow workers: one deployment per project, pinned runs.

Each project's workflow worker polls `wf-<project>` as version `<factory SHA>+<home SHA>` of deployment
`wf-<project>`, with `PINNED` as the default behaviour, so a run finishes on the version it started on. On start the
worker makes its version the deployment's current one, so new runs go to it.
"""

import asyncio

from fact.core.logging import get_logger
from fact.worker.loader import ProjectWorkflows
from temporalio.api.workflowservice.v1 import SetWorkerDeploymentCurrentVersionRequest
from temporalio.client import Client
from temporalio.common import VersioningBehavior, WorkerDeploymentVersion
from temporalio.service import RPCError
from temporalio.worker import WorkerDeploymentConfig

log = get_logger(__name__)

SET_CURRENT_ATTEMPTS = 10
SET_CURRENT_DELAY_S = 0.5
IDENTITY = "fact-worker"


def deployment_config(loaded: ProjectWorkflows) -> WorkerDeploymentConfig:
    version = WorkerDeploymentVersion(deployment_name=loaded.deployment_name, build_id=loaded.version)
    return WorkerDeploymentConfig(
        version=version, use_worker_versioning=True, default_versioning_behavior=VersioningBehavior.PINNED
    )


async def set_current_version(client: Client, loaded: ProjectWorkflows, delay_s: float = SET_CURRENT_DELAY_S) -> None:
    """
    Make the worker's version current for its deployment; retried while the server registers the new version.

    Runs pinned to an older version keep waiting for a worker of that version (drop and restart them to move on).
    """
    request = SetWorkerDeploymentCurrentVersionRequest(
        namespace=client.namespace,
        deployment_name=loaded.deployment_name,
        build_id=loaded.version,
        identity=IDENTITY,
        allow_no_pollers=True,
    )
    for attempt in range(1, SET_CURRENT_ATTEMPTS + 1):
        try:
            await client.workflow_service.set_worker_deployment_current_version(request)
        except RPCError as exc:
            if attempt == SET_CURRENT_ATTEMPTS:
                raise
            log.debug("set_current_version_retry", deployment=loaded.deployment_name, error=str(exc))
            await asyncio.sleep(delay_s * attempt)
        else:
            log.info("current_version_set", deployment=loaded.deployment_name, version=loaded.version)
            return
