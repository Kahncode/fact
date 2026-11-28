"""
What the CLI changes in a real Temporal dev server: the namespace retention, and drained versions deleted.

Needs the `temporal` CLI (dev server): the time-skipping test server has no Worker Deployment Versioning.
"""

import asyncio
import shutil
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from fact.cli import temporal_admin
from fact.cli.dev_commands import temporal_endpoint
from fact.core.settings import Settings
from temporalio.testing import WorkflowEnvironment
from test_drainage import FAST_DRAINAGE, _make_current, _until, _worker

TEMPORAL = shutil.which("temporal")
# a stopped worker counts as a poller for minutes by default, and Temporal refuses to delete its version meanwhile
FAST_POLLERS = ["--dynamic-config-value", 'matching.PollerHistoryTTL="1s"']

pytestmark = pytest.mark.skipif(TEMPORAL is None, reason="the temporal CLI is not on PATH")


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    started = await WorkflowEnvironment.start_local(
        dev_server_existing_path=TEMPORAL, dev_server_extra_args=[*FAST_DRAINAGE, *FAST_POLLERS]
    )
    async with started:
        yield started


def _settings(env: WorkflowEnvironment, **temporal: object) -> Settings:
    return Settings(temporal={"address": env.client.service_client.config.target_host, **temporal})


async def test_fact_dev_sets_the_namespace_retention(env: WorkflowEnvironment) -> None:
    settings = _settings(env, retention_days=7)
    setter = temporal_admin.NamespaceRetention(settings, temporal_endpoint(settings), log=lambda _line: None)

    await asyncio.to_thread(setter)

    assert setter.done
    assert await temporal_admin.retention(settings) == timedelta(days=7)


async def test_a_drained_version_is_deleted_and_the_current_one_refused(env: WorkflowEnvironment) -> None:
    settings = _settings(env)
    queue = f"wf-clean-{uuid.uuid4().hex[:8]}"
    async with _worker(env.client, queue, "old"):
        await _make_current(env.client, queue, "old")
    async with _worker(env.client, queue, "new"):
        await _make_current(env.client, queue, "new")
    versions = [(queue, "old"), (queue, "new"), (queue, "never")]

    async def deleted() -> bool:
        errors = await asyncio.to_thread(temporal_admin.delete_versions, settings, versions)
        return errors.keys() == {(queue, "new")}  # the current version stays; one never polled counts as gone

    await _until(deleted)  # once Temporal sees `old` drained and without pollers
