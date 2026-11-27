"""Making a project's version current."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fact.worker.deployments import SET_CURRENT_ATTEMPTS, set_current_version
from fact.worker.loader import ProjectWorkflows
from temporalio.service import RPCError, RPCStatusCode

VERSION = "aaaaaaaaaaaa+" + "b" * 40


def _loaded() -> ProjectWorkflows:
    return ProjectWorkflows("acme", "acme/home", "b" * 40, Path("checkout"), VERSION, ())


def _client(side_effect: list[object]) -> MagicMock:
    client = MagicMock()
    client.namespace = "default"
    client.workflow_service.set_worker_deployment_current_version = AsyncMock(side_effect=side_effect)
    return client


async def test_set_current_version_retries_until_the_version_exists() -> None:
    not_yet = RPCError("version not found", RPCStatusCode.NOT_FOUND, b"")
    client = _client([not_yet, None])

    await set_current_version(client, _loaded(), delay_s=0)

    assert client.workflow_service.set_worker_deployment_current_version.await_count == 2  # noqa: PLR2004


async def test_set_current_version_gives_up() -> None:
    error = RPCError("down", RPCStatusCode.UNAVAILABLE, b"")
    client = _client([error] * SET_CURRENT_ATTEMPTS)

    with pytest.raises(RPCError, match="down"):
        await set_current_version(client, _loaded(), delay_s=0)
