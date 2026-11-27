"""
The retired `fact-scheduler` Schedule is deleted at worker start, and a missing one is no error.

On the local dev server: the time-skipping server does not implement Schedules.
"""

from datetime import timedelta

import pytest
from fact.core.scheduler import SCHEDULE_ID
from fact.worker.scheduler import delete_leftover_schedule
from temporalio.client import Schedule, ScheduleActionStartWorkflow, ScheduleIntervalSpec, ScheduleSpec
from temporalio.service import RPCError
from temporalio.testing import WorkflowEnvironment


async def test_leftover_schedule_is_deleted() -> None:
    async with await WorkflowEnvironment.start_local() as env:
        schedule = Schedule(
            action=ScheduleActionStartWorkflow("SchedulerTick", id="tick", task_queue="control"),
            spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(seconds=30))]),
        )
        await env.client.create_schedule(SCHEDULE_ID, schedule)
        assert await delete_leftover_schedule(env.client) is True
        with pytest.raises(RPCError):
            await env.client.get_schedule_handle(SCHEDULE_ID).describe()
        assert await delete_leftover_schedule(env.client) is False  # the next start: nothing left, no error
