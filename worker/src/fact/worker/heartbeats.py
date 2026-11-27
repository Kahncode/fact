"""
Framework heartbeats: every activity the worker runs heartbeats on a timer while it runs, with no code of its own.

`HeartbeatInterceptor` is passed to every activity worker. It sends a heartbeat every `HEARTBEAT_INTERVAL`, so an
attempt whose worker is gone times out after `HEARTBEAT_TIMEOUT` and a cancellation (a stop) reaches the activity.
An activity that reports progress hands its details to `report` (the agent step's turn count and session id); each
beat carries the last details reported. Until an attempt reports, it beats with its previous attempt's last details,
so a retry keeps what the attempt before it reported (`wait_for_ci` keeps its deadline this way).
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

from fact.core.activity_contract import HEARTBEAT_INTERVAL
from temporalio import activity
from temporalio.worker import ActivityInboundInterceptor, ExecuteActivityInput, Interceptor


class _Details:
    def __init__(self, details: tuple[Any, ...]) -> None:
        self.details = details


_current: ContextVar[_Details | None] = ContextVar("fact_heartbeat_details", default=None)


def report(*details: Any) -> None:  # noqa: ANN401 - heartbeat details are any serializable values
    """Hand the running activity's progress to its heartbeats; outside the wrapper (a unit test) it does nothing."""
    holder = _current.get()
    if holder is not None:
        holder.details = details


def previous_details() -> tuple[Any, ...]:
    """Return the last heartbeat details of the attempt before this one, as Temporal kept them; () on a first one."""
    return tuple(activity.info().heartbeat_details) if activity.in_activity() else ()


async def _beat(holder: _Details, interval_s: float) -> None:
    while True:
        activity.heartbeat(*holder.details)
        await asyncio.sleep(interval_s)


async def heartbeating[T](run: Callable[[], Awaitable[T]], interval_s: float | None = None) -> T:
    """Run `run()` inside the current activity, heartbeating every `interval_s` (`HEARTBEAT_INTERVAL`) until it ends."""
    holder = _Details(previous_details())
    token = _current.set(holder)
    beat = asyncio.create_task(_beat(holder, interval_s or HEARTBEAT_INTERVAL.total_seconds()))
    try:
        return await run()
    finally:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat
        _current.reset(token)


class _Inbound(ActivityInboundInterceptor):
    def __init__(self, nxt: ActivityInboundInterceptor, interval_s: float | None) -> None:
        super().__init__(nxt)
        self._interval_s = interval_s

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:  # noqa: A002, ANN401 - Temporal's signature
        return await heartbeating(lambda: super(_Inbound, self).execute_activity(input), self._interval_s)


class HeartbeatInterceptor(Interceptor):
    """Wrap every activity of a worker in `heartbeating`."""

    def __init__(self, interval_s: float | None = None) -> None:
        self._interval_s = interval_s

    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:  # noqa: A002
        return _Inbound(next, self._interval_s)
