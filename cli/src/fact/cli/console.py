"""Console output of `fact dev`: every line prefixed by the process it comes from, children's read from their logs."""

import sys
import threading
import time
from pathlib import Path

POLL_S = 0.2
PUMP_JOIN_S = 2.0

_print_lock = threading.Lock()


def emit(name: str, width: int, line: str) -> None:
    with _print_lock:
        sys.stdout.write(f"{name:<{width}} | {line.rstrip()}\n")
        sys.stdout.flush()


class LogTail(threading.Thread):
    """Print the lines a child appends to its log, from `offset`, until `finish` and the end of the file."""

    def __init__(self, name: str, width: int, path: Path, offset: int) -> None:
        super().__init__(name=f"tail-{name}", daemon=True)
        self._child, self._width, self._path, self._offset = name, width, path, offset
        self._done = threading.Event()

    def run(self) -> None:
        try:
            log = self._path.open("rb")
        except OSError:  # an adopted child whose log was deleted
            return
        with log:
            log.seek(self._offset)
            pending = b""
            while True:
                if chunk := log.read(65536):
                    *lines, pending = (pending + chunk).split(b"\n")
                    for line in lines:
                        emit(self._child, self._width, line.decode("utf-8", "replace"))
                elif self._done.is_set():
                    break
                else:
                    time.sleep(POLL_S)
            if pending:
                emit(self._child, self._width, pending.decode("utf-8", "replace"))

    def finish(self) -> None:
        self._done.set()
        self.join(timeout=PUMP_JOIN_S)
