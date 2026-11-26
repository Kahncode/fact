"""Artifact store: files (logs, transcripts, diffs) kept outside the DB and addressed by key."""

import asyncio
import os
import shutil
import tempfile
from collections.abc import AsyncIterable, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO, Protocol, runtime_checkable

TMP_DIR = ".tmp"
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


class InvalidArtifactKeyError(ValueError):
    """The key is empty, absolute, or would leave the store root."""


@runtime_checkable
class ArtifactStore(Protocol):
    """Backend-neutral interface; an S3 backend can implement it later."""

    def put_bytes(self, key: str, data: bytes) -> None: ...

    def put_file(self, key: str, path: Path) -> None: ...

    def open(self, key: str) -> BinaryIO: ...

    def exists(self, key: str) -> bool: ...


def step_key(run_id: str, stage_id: str, attempt: int, name: str) -> str:
    """Key `runs/<run>/<stage>/<attempt>/<name>` for a file produced by a step attempt."""
    key = "/".join(("runs", run_id, stage_id, str(attempt), name))
    validate_key(key)
    return key


def handoff_key(run_id: str) -> str:
    """Key `runs/<run>/handoff.md` of the note a stopped run leaves for the next run of its task."""
    key = f"runs/{run_id}/handoff.md"
    validate_key(key)
    return key


def validate_key(key: str) -> str:
    """Return the key if it is a safe relative POSIX-style path, else raise `InvalidArtifactKeyError`."""
    if not key or "\x00" in key:
        msg = "Artifact key is empty or contains a NUL byte"
        raise InvalidArtifactKeyError(msg)
    # Check both flavours so a Windows-style key is refused on every OS.
    for flavour in (PurePosixPath(key), PureWindowsPath(key)):
        if flavour.is_absolute() or flavour.anchor or flavour.drive:
            msg = f"Artifact key must be relative: {key!r}"
            raise InvalidArtifactKeyError(msg)
    parts = key.replace("\\", "/").split("/")
    for part in parts:
        if not part or part.startswith("."):
            msg = f"Artifact key has an empty or dot-leading segment: {key!r}"
            raise InvalidArtifactKeyError(msg)
        if part.endswith((".", " ")) or part.split(".", 1)[0].upper() in _RESERVED:
            msg = f"Artifact key has a segment that is not portable to Windows: {key!r}"
            raise InvalidArtifactKeyError(msg)
    if ":" in key:
        msg = f"Artifact key must not contain ':': {key!r}"
        raise InvalidArtifactKeyError(msg)
    return key


class LocalArtifactStore:
    """Stores artifacts as files under `root`."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def path_for(self, key: str) -> Path:
        """Filesystem path of a key; it is always inside the root."""
        validate_key(key)
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            msg = f"Artifact key escapes the store root: {key!r}"
            raise InvalidArtifactKeyError(msg)
        return path

    @contextmanager
    def _atomic(self, key: str) -> Iterator[tuple[BinaryIO, Path]]:
        """Yield a temp file under `<root>/.tmp`; on success it replaces the target, readers never see a partial file."""
        dest = self.path_for(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_dir = self.root / TMP_DIR
        tmp_dir.mkdir(exist_ok=True)
        fd, name = tempfile.mkstemp(dir=tmp_dir)
        tmp = Path(name)
        try:
            with os.fdopen(fd, "wb") as f:
                yield f, dest
            tmp.replace(dest)
        finally:
            tmp.unlink(missing_ok=True)

    def put_bytes(self, key: str, data: bytes) -> None:
        with self._atomic(key) as (f, _):
            f.write(data)

    def put_file(self, key: str, path: Path) -> None:
        with self._atomic(key) as (f, _), path.open("rb") as src:
            shutil.copyfileobj(src, f)

    async def put_stream(self, key: str, chunks: AsyncIterable[bytes]) -> None:
        with self._atomic(key) as (f, _):
            async for chunk in chunks:
                await asyncio.to_thread(f.write, chunk)

    def open(self, key: str) -> BinaryIO:
        return self.path_for(key).open("rb")

    def exists(self, key: str) -> bool:
        return self.path_for(key).is_file()
