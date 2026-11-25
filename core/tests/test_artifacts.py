from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fact.core.artifacts import ArtifactStore, InvalidArtifactKeyError, LocalArtifactStore, step_key

BAD_KEYS = [
    "../x",
    "a/../../x",
    "/etc/passwd",
    "C:\\x",
    "C:/x",
    "\\\\host\\share\\x",
    "..\\x",
    "a/./b",
    "a//b",
    "",
    "a\x00b",
]


def test_step_key() -> None:
    assert step_key("r1", "implement", 2, "output.log") == "runs/r1/implement/2/output.log"


def test_round_trip(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "art")
    assert isinstance(store, ArtifactStore)
    key = step_key("r1", "s", 1, "output.log")
    assert not store.exists(key)
    store.put_bytes(key, b"hello")
    assert store.exists(key)
    with store.open(key) as f:
        assert f.read() == b"hello"


def test_put_file(tmp_path: Path) -> None:
    src = tmp_path / "src.txt"
    src.write_bytes(b"data")
    store = LocalArtifactStore(tmp_path / "art")
    store.put_file("a/b.txt", src)
    with store.open("a/b.txt") as f:
        assert f.read() == b"data"


@pytest.mark.parametrize("key", BAD_KEYS)
def test_bad_keys_rejected(tmp_path: Path, key: str) -> None:
    store = LocalArtifactStore(tmp_path / "art")
    with pytest.raises(InvalidArtifactKeyError):
        store.put_bytes(key, b"x")
    with pytest.raises(InvalidArtifactKeyError):
        store.exists(key)
    assert not (tmp_path / "x").exists()


def test_step_key_rejects_escape() -> None:
    with pytest.raises(InvalidArtifactKeyError):
        step_key("..", "s", 1, "x")


@pytest.mark.parametrize("key", ["runs/NUL", "runs/con.log", "a./b", "a /b", ".tmp/x", "runs/.hidden"])
def test_windows_and_hidden_names_rejected(tmp_path: Path, key: str) -> None:
    with pytest.raises(InvalidArtifactKeyError):
        LocalArtifactStore(tmp_path).put_bytes(key, b"x")


def test_overwrite_leaves_no_temp(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    store.put_bytes("a/b.log", b"one")
    store.put_bytes("a/b.log", b"two")
    with store.open("a/b.log") as f:
        assert f.read() == b"two"
    assert not list((tmp_path / ".tmp").iterdir())


async def test_failed_stream_leaves_nothing(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)

    async def chunks() -> AsyncIterator[bytes]:
        yield b"partial"
        raise ConnectionError

    with pytest.raises(ConnectionError):
        await store.put_stream("a/b.log", chunks())
    assert not store.exists("a/b.log")
    assert not list((tmp_path / ".tmp").iterdir())


def test_bad_keys_on_put_file_and_open(tmp_path: Path) -> None:
    src = tmp_path / "s"
    src.write_bytes(b"x")
    store = LocalArtifactStore(tmp_path / "art")
    with pytest.raises(InvalidArtifactKeyError):
        store.put_file("../x", src)
    with pytest.raises(InvalidArtifactKeyError):
        store.open("../x")
