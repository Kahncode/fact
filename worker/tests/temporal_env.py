"""One time-skipping Temporal test server per test module: starting one costs about half a second."""

import contextlib

from standard_env import IDENTITY
from temporalio.client import WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.service import RPCError
from temporalio.testing import WorkflowEnvironment

_env: WorkflowEnvironment | None = None


async def time_skipping_env() -> WorkflowEnvironment:
    """Start the shared server on first use; `conftest.py` shuts it down at the end of the module."""
    global _env  # noqa: PLW0603 - a per-process singleton, like the server it holds
    if _env is None:
        _env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter, identity=IDENTITY)
    return _env


async def shutdown() -> None:
    global _env  # noqa: PLW0603
    if _env is not None:
        await _env.shutdown()
        _env = None


async def terminate_if_running(handle: WorkflowHandle) -> None:
    """End a workflow a test left running, so its timers cannot move the shared clock for the next test."""
    with contextlib.suppress(RPCError):  # already closed
        await handle.terminate(reason="test ended")
