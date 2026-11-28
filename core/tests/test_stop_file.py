import threading
from pathlib import Path

from fact.core.stop_file import watch_stop_file


def test_the_watch_calls_back_once_the_stop_file_appears(tmp_path: Path) -> None:
    # The API's uvicorn server exits through this callback (`server.should_exit`).
    path = tmp_path / "api.stop"
    stopped = threading.Event()

    thread = watch_stop_file(path, stopped.set, interval=0.01)
    path.touch()

    assert stopped.wait(5)
    thread.join(5)
