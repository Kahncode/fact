"""Trivial workflow and activity that prove the worker round trip."""

from datetime import timedelta

from fact.core.activity_contract import CONTROL_QUEUE, SANDBOX_QUEUE
from temporalio import activity, workflow

__all__ = ["CONTROL_QUEUE", "SANDBOX_QUEUE", "PingWorkflow", "ping"]


@activity.defn
async def ping() -> str:
    return "pong"


@workflow.defn
class PingWorkflow:
    @workflow.run
    async def run(self) -> str:
        return await workflow.execute_activity(ping, start_to_close_timeout=timedelta(seconds=10))
