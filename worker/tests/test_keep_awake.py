"""Keep-awake: held while runs are active, released when none are or the worker stops; a no-op off Windows."""

import asyncio
import sys
import threading
from unittest import mock

import httpx
import pytest
from fact.worker import keep_awake
from fact.worker.keep_awake import ES_CONTINUOUS, ES_SYSTEM_REQUIRED, KeepAwake, api_active_runs, watch_active_runs

HOLD = ES_CONTINUOUS | ES_SYSTEM_REQUIRED


class Kernel32:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []  # (flags, thread id)

    def SetThreadExecutionState(self, flags: int) -> int:  # noqa: N802 - the Windows name
        self.calls.append((flags, threading.get_ident()))
        return 1


def test_holds_and_releases_only_on_change() -> None:
    kernel = Kernel32()
    keeper = KeepAwake(platform="win32", kernel32=kernel)
    for active in (True, True, False, False, True):
        keeper.set(active=active)
    assert [flags for flags, _ in kernel.calls] == [HOLD, ES_CONTINUOUS, HOLD]
    assert keeper.held


@pytest.mark.parametrize(("platform", "enabled"), [("linux", True), ("darwin", True), ("win32", False)])
def test_noop_off_windows_or_disabled(platform: str, *, enabled: bool) -> None:
    kernel = Kernel32()
    KeepAwake(enabled=enabled, platform=platform, kernel32=kernel).set(active=True)
    assert kernel.calls == []


async def test_watch_holds_while_runs_are_active_on_the_loop_thread() -> None:
    kernel = Kernel32()
    keeper = KeepAwake(platform="win32", kernel32=kernel)
    counts = iter([0, 2, 1, 0, 3, "bad"])
    polled = asyncio.Event()

    async def count() -> int:
        try:
            return int(next(counts))  # "bad": a ValueError, logged like an unreachable API
        except StopIteration:
            polled.set()
            msg = "API gone"
            raise httpx.ConnectError(msg) from None  # keeps the state, logs a warning

    task = asyncio.create_task(watch_active_runs(keeper, count, interval=0))
    await asyncio.wait_for(polled.wait(), 5)
    assert keeper.held  # the last count was 3; an unreachable API changes nothing
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [flags for flags, _ in kernel.calls] == [HOLD, ES_CONTINUOUS, HOLD, ES_CONTINUOUS]  # released on stop
    assert {thread for _, thread in kernel.calls} == {threading.get_ident()}  # never from a pool thread


@pytest.mark.skipif(sys.platform != "win32", reason="SetThreadExecutionState is Windows only")
def test_calls_windows_through_ctypes() -> None:
    fake = mock.MagicMock()
    fake.kernel32.SetThreadExecutionState.return_value = 1
    with mock.patch.object(keep_awake.ctypes, "windll", fake):
        keeper = KeepAwake()
        keeper.set(active=True)
        keeper.set(active=False)
    assert [c.args for c in fake.kernel32.SetThreadExecutionState.call_args_list] == [(HOLD,), (ES_CONTINUOUS,)]


async def test_api_active_runs() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/scheduler/active"
        return httpx.Response(200, json={"active": 2})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await api_active_runs("http://api/", http)() == 2  # noqa: PLR2004
