"""
`<data_dir>/fact-dev.lock`: one `fact dev` per data directory, pinned or from the source tree.

An OS file lock, so the OS releases it when its process dies however it dies; the file holds the holder's pid for the
message a second `fact dev` prints. On Windows the lock covers one byte far past the pid, so the pid stays readable.

`<data_dir>/fact-dev.pin` names the pin a `fact dev --in-pin` holding the lock runs from (its code and `.venv`), so
`fact cleanup` keeps that pin: it has no process record, and it stays on an older pin while newer ones leave `cli/`
and `core/` alone. The holder writes it (or deletes it, from the source tree) after taking the lock; it counts only
while the lock is held.
"""

import contextlib
import os
import sys
from pathlib import Path
from typing import IO

LOCK_NAME = "fact-dev.lock"
PIN_NAME = "fact-dev.pin"
_WINDOWS_LOCK_OFFSET = 1 << 20


class AlreadyRunningError(RuntimeError):
    def __init__(self, path: Path, pid: str) -> None:
        self.pid = pid
        holder = f"pid {pid}" if pid else "another process"
        super().__init__(f"fact dev is already running ({holder}, holding {path})")


def _try_lock(fh: IO[bytes]) -> bool:
    try:
        if sys.platform == "win32":
            import msvcrt  # noqa: PLC0415

            fh.seek(_WINDOWS_LOCK_OFFSET)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl  # noqa: PLC0415

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


class DevLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: IO[bytes] | None = None

    def acquire(self) -> None:
        """Take the lock, or raise `AlreadyRunningError` naming the pid holding it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        fh = self.path.open("r+b")
        if not _try_lock(fh):
            fh.close()
            raise AlreadyRunningError(self.path, self._holder())
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()).encode())
        fh.flush()
        self._fh = fh

    def held(self) -> bool:
        """Return whether another process holds the lock now; creates and changes nothing."""
        try:
            fh = self.path.open("r+b")
        except FileNotFoundError:
            return False
        with fh:
            return not _try_lock(fh)  # closing the file releases a lock taken here

    def _holder(self) -> str:
        with contextlib.suppress(OSError):
            return self.path.read_text(encoding="utf-8").strip()
        return ""

    def release(self) -> None:
        """Close the file, which releases the lock; the file stays (deleting it would race a new holder)."""
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def record_pin(data_dir: Path, pin: str | None) -> None:
    """Name the pin the `fact dev` holding the lock runs from; None (from the source tree) clears it."""
    path = data_dir / PIN_NAME
    if pin is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(f"{pin}\n", encoding="utf-8")


def running_pin(data_dir: Path) -> str | None:
    """Return the pin a running `fact dev` runs from, None when none runs or it runs from the source tree."""
    if not DevLock(data_dir / LOCK_NAME).held():
        return None
    with contextlib.suppress(OSError):
        return (data_dir / PIN_NAME).read_text(encoding="utf-8").strip() or None
    return None
