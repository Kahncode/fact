"""
Dev only: the loop that ticks the scheduler every `concurrency.scheduler_interval_seconds` while it is on.

Started in the FastAPI lifespan when `settings.is_dev_environment()`; outside dev nothing ticks by itself and an
outside cron (or a webhook) calls `POST /scheduler/tick`. The loop sleeps first, then ticks if the scheduler is on
(`SchedulerRuntime.on`, flipped by `fact scheduler on|off`). A tick that raises is logged and the loop goes on;
cancelling the task stops it.
"""

import asyncio
from collections.abc import Awaitable, Callable

from fact.api.scheduler import SchedulerRuntime, run_tick
from fact.core.logging import get_logger
from fact.core.scheduler import TickResult
from starlette.datastructures import State

log = get_logger(__name__)


async def scheduler_loop(
    state: State,
    *,
    tick: Callable[[State], Awaitable[TickResult]] = run_tick,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    runtime: SchedulerRuntime = state.scheduler
    interval = state.settings.concurrency.scheduler_interval_seconds
    while True:
        await sleep(interval)
        if not runtime.on:
            continue
        try:
            result = await tick(state)
        except Exception:
            log.exception("scheduler_tick_failed")
            continue
        if result.started or result.failed or result.usage_full:
            log.info("scheduler_ticked", started=result.started, failed=result.failed, usage_full=result.usage_full)
