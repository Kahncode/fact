import asyncio
from pathlib import Path

import pytest
from fact.core.stop_file import STOP_FILE_VAR
from fact.worker import __main__ as worker_main


async def test_the_stop_file_cancels_the_worker_as_ctrl_c_does(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Cancelling `run` exits the workers' context, whose graceful shutdown ends an agent step AGENT_INTERRUPTED.
    path = tmp_path / "worker.stop"
    monkeypatch.setenv(STOP_FILE_VAR, str(path))
    running = asyncio.Event()

    async def run() -> None:
        worker_main._cancel_on_stop_file()  # noqa: SLF001
        running.set()
        await asyncio.sleep(30)

    task = asyncio.create_task(run())
    await running.wait()
    path.touch()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
