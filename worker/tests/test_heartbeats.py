"""Framework heartbeats: an activity with no heartbeat code heartbeats while it runs, with what it reported."""

import asyncio
import dataclasses
import uuid
from datetime import timedelta
from typing import Any

from fact.worker.heartbeats import HeartbeatInterceptor, heartbeating, report
from standard_env import OPTIONS
from temporal_env import time_skipping_env
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.testing import ActivityEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

INTERVAL_S = 0.05
QUEUE = "heartbeats-test"


@activity.defn(name="quiet")
async def quiet() -> str:
    """No heartbeat code of its own: without the framework's beats it outlives its heartbeat timeout."""
    await asyncio.sleep(0.75)  # 1.5 heartbeat timeouts
    return "done"


@workflow.defn(name="calls-quiet", sandboxed=False)
class CallsQuiet:
    @workflow.run
    async def run(self) -> str:
        return await workflow.execute_activity(
            "quiet",
            result_type=str,
            start_to_close_timeout=timedelta(seconds=30),
            heartbeat_timeout=timedelta(seconds=0.5),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )


async def test_an_activity_without_heartbeat_code_outlives_its_heartbeat_timeout() -> None:
    env = await time_skipping_env()
    worker = Worker(
        env.client,
        task_queue=QUEUE,
        workflows=[CallsQuiet],
        activities=[quiet],
        workflow_runner=UnsandboxedWorkflowRunner(),
        interceptors=[HeartbeatInterceptor(INTERVAL_S)],
        **OPTIONS,
    )
    async with worker:
        result = await env.client.execute_workflow(CallsQuiet.run, id=f"hb-{uuid.uuid4()}", task_queue=QUEUE)
    assert result == "done"


async def test_beats_carry_the_last_reported_details() -> None:
    reported = asyncio.Event()

    async def work() -> None:
        report("step: 1 turns", "s-1")
        reported.set()
        await asyncio.sleep(INTERVAL_S * 4)

    beats: list[tuple[Any, ...]] = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *details: beats.append(details)
    await env.run(heartbeating, work, INTERVAL_S)
    assert reported.is_set()
    assert beats[-1] == ("step: 1 turns", "s-1")


async def test_a_retry_beats_with_the_previous_attempts_details_until_it_reports() -> None:
    beats: list[tuple[Any, ...]] = []
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, heartbeat_details=[123.0], attempt=2)
    env.on_heartbeat = lambda *details: beats.append(details)
    await env.run(heartbeating, lambda: asyncio.sleep(0), INTERVAL_S)
    assert beats == [(123.0,)]
