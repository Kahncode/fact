"""
A process `fact dev` starts stops gracefully once its stop file appears.

`fact dev` passes the path in `FACT_STOP_FILE` (`<data_dir>/run/<name>.stop`) and writes the file to ask the process
to stop: Windows has no SIGTERM for a detached process, and a file works the same on every platform. The API and the
worker watch it; a process started without the variable watches nothing.
"""

import asyncio
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

STOP_FILE_VAR = "FACT_STOP_FILE"
POLL_S = 0.5


def stop_file_path() -> Path | None:
    """Return the stop file this process watches, or None when it was not started by `fact dev`."""
    value = os.environ.get(STOP_FILE_VAR)
    return Path(value) if value else None


async def wait_for_stop_file(path: Path, interval: float = POLL_S) -> None:
    """Return once `path` exists."""
    while not path.exists():  # noqa: ASYNC110, ASYNC240 - one stat per poll; a file has no event to await
        await asyncio.sleep(interval)


def watch_stop_file(path: Path, on_stop: Callable[[], None], interval: float = POLL_S) -> threading.Thread:
    """Call `on_stop` once `path` exists, from a daemon thread (for code that does not own its event loop)."""

    def watch() -> None:
        while not path.exists():
            time.sleep(interval)
        on_stop()

    thread = threading.Thread(target=watch, name="stop-file", daemon=True)
    thread.start()
    return thread
