import os
from pathlib import Path

import pytest
from fact.cli.dev_lock import AlreadyRunningError, DevLock


def test_a_second_holder_is_refused_with_the_first_ones_pid(tmp_path: Path) -> None:
    first = DevLock(tmp_path / "fact-dev.lock")
    first.acquire()
    try:
        with pytest.raises(AlreadyRunningError, match=f"pid {os.getpid()}"):
            DevLock(tmp_path / "fact-dev.lock").acquire()
        assert DevLock(tmp_path / "fact-dev.lock").held()
    finally:
        first.release()
    assert not DevLock(tmp_path / "fact-dev.lock").held()

    again = DevLock(tmp_path / "fact-dev.lock")
    again.acquire()  # released with its holder
    again.release()
