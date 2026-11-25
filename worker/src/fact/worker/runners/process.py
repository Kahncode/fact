"""Subprocess helpers: the inherited environment, merged output and killing a whole process tree."""

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from fact.worker.runners.base import ExecExit, ExecLine, ExecOutput

_LINE_LIMIT = 1 << 20
_IS_WINDOWS = sys.platform == "win32"


def build_env(extra: Mapping[str, str] | None) -> dict[str, str]:
    """Return the worker's own environment plus what the caller passes: a local machine is a trusted environment."""
    return {**os.environ, **(extra or {})}


async def kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill the process and every descendant. On Windows a descendant of an already-dead leader is not reachable."""
    if _IS_WINDOWS:
        if proc.returncode is None:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(proc.pid),
                "/T",
                "/F",
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            await killer.wait()
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)  # the group outlives its leader while any member runs
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


async def spawn(  # noqa: PLR0913 - options are keyword-only
    argv: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    *,
    pipe_stdin: bool = False,
    merge_stderr: bool = True,
    limit: int = _LINE_LIMIT,
) -> asyncio.subprocess.Process:
    """
    Start `argv` in its own process group so the tree can be killed.

    By default stdin is closed and stderr is merged into stdout; `pipe_stdin` and `merge_stderr=False` give separate
    pipes instead. `limit` caps the length of one output line.
    """
    group: dict[str, Any] = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if _IS_WINDOWS else {"start_new_session": True}
    )
    return await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=dict(env),
        stdin=asyncio.subprocess.PIPE if pipe_stdin else subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE,
        limit=limit,
        **group,
    )


async def _read_line(proc: asyncio.subprocess.Process, remaining: float | None) -> bytes:
    assert proc.stdout is not None  # noqa: S101
    try:
        return await asyncio.wait_for(proc.stdout.readline(), remaining)
    except ValueError:  # a line longer than the stream limit; the oversized chunk is already discarded
        return b"[line too long, dropped]\n"


async def stream(
    proc: asyncio.subprocess.Process,
    timeout: float | None,  # noqa: ASYNC109
) -> AsyncIterator[ExecOutput]:
    """Yield output lines, then an `ExecExit`. On timeout or cancellation the whole tree is killed."""
    deadline = None if timeout is None else time.monotonic() + timeout
    timed_out = False

    def remaining() -> float | None:
        return None if deadline is None else max(deadline - time.monotonic(), 0.0)

    try:
        try:
            while raw := await _read_line(proc, remaining()):
                yield ExecLine(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
            await asyncio.wait_for(proc.wait(), remaining())  # stdout can close while the process keeps running
        except TimeoutError:
            timed_out = True
    finally:
        await kill_tree(proc)  # also runs when the consumer stops early or the task is cancelled
    await proc.wait()
    yield ExecExit(exit_code=-1 if timed_out else proc.returncode or 0, timed_out=timed_out)
