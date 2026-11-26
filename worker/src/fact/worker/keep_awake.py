"""
Keep Windows awake while runs are active (`worker.keep_awake`); a no-op on other platforms.

`SetThreadExecutionState` holds only while the calling thread lives, so `KeepAwake.set` must be called from one
long-lived thread: `watch_active_runs` runs on the worker's event-loop thread, never through `asyncio.to_thread` or an
executor pool.
"""

import asyncio
import ctypes
import sys
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fact.core.logging import get_logger

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
POLL_SECONDS = 60.0

log = get_logger(__name__)


def _kernel32() -> Any:  # noqa: ANN401 - ctypes' WinDLL
    return ctypes.windll.kernel32  # type: ignore[attr-defined]


class KeepAwake:
    """Hold or release the system-required state; calls Windows only when the wanted state changes."""

    def __init__(self, *, enabled: bool = True, platform: str = sys.platform, kernel32: object = None) -> None:
        self._enabled = enabled and platform == "win32"
        self._kernel32 = kernel32
        self.held = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set(self, *, active: bool) -> None:
        if not self._enabled or active == self.held:
            return
        kernel32: Any = self._kernel32 or _kernel32()
        flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED if active else ES_CONTINUOUS
        if not kernel32.SetThreadExecutionState(flags):
            log.warning("keep_awake_failed", active=active)
            return
        self.held = active
        log.info("keep_awake_held" if active else "keep_awake_released")


async def watch_active_runs(
    keeper: KeepAwake, count_active: Callable[[], Awaitable[int]], interval: float = POLL_SECONDS
) -> None:
    """Hold while `count_active()` is above zero, until cancelled; then release. An unreachable API keeps the state."""
    if not keeper.enabled:  # nothing to hold: no polling either
        await asyncio.Event().wait()
    try:
        while True:
            try:
                keeper.set(active=await count_active() > 0)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:  # a side feature never stops the worker
                log.warning("keep_awake_poll_failed", error=repr(exc))
            await asyncio.sleep(interval)
    finally:
        keeper.set(active=False)


def api_active_runs(api_base_url: str, http: httpx.AsyncClient) -> Callable[[], Awaitable[int]]:
    """Count runs queued, running or waiting through `GET /api/v1/scheduler/active`."""
    url = api_base_url.rstrip("/") + "/api/v1/scheduler/active"

    async def count() -> int:
        response = await http.get(url)
        response.raise_for_status()
        return int(response.json()["active"])

    return count
