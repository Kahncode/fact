"""
Worker generations: a factory pin with each project's home SHA, served by one worker process each.

A generation's record is `<data_dir>/run/generations/<id>.json`. `fact dev` creates it, with the factory SHA, when it
starts a pin's worker for the first time; that worker (`python -m fact.worker --generation <id>`) records each
project's home SHA once it has loaded it, so the generation loads the same SHAs at every later start. Ids are
`<n>-<factory SHA[:12]>`, `n` counting up, so the newest generation is the one with the greatest `n`: only it makes
its versions current (`docs/spec/14-dogfooding-and-safety.md`, Upgrading while it runs). A new record comes with
`<id>.starting`, which its worker removes once through its start: a newest generation whose worker exits while it is
there failed to start, and `fact dev` deletes it so the one before stays current.
"""

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from fact.core.activity_contract import workflow_queue

GENERATIONS_SUBDIR = Path("run") / "generations"
STARTING_SUFFIX = ".starting"  # `<id>.starting`: recorded, its worker not through its start yet
SHORT_SHA = 12  # the factory part of a version id; the home SHA stays whole
_ID = re.compile(r"^(\d+)-([0-9a-f]{12}|unknown)$")


class GenerationError(RuntimeError):
    """A generation record is missing, unreadable or its id is invalid."""


def version_id(factory_sha: str, home_sha: str) -> str:
    """Return `<factory SHA, 12 chars>+<home SHA>`: the build id of a project's worker deployment version."""
    return f"{factory_sha[:SHORT_SHA]}+{home_sha}"


@dataclass(frozen=True)
class Generation:
    id: str
    factory: str  # the factory SHA (`pinned/factory/<sha>`)
    projects: dict[str, str] = field(default_factory=dict)  # project name to home SHA, as its worker loaded it

    @property
    def number(self) -> int:
        return _number(self.id)

    def versions(self) -> list[tuple[str, str]]:
        """Return the (deployment, build id) of each recorded project's version."""
        return [(workflow_queue(p), version_id(self.factory, sha)) for p, sha in sorted(self.projects.items())]


def _number(generation_id: str) -> int:
    match = _ID.match(generation_id)
    if match is None:
        msg = f"invalid generation id {generation_id!r}"
        raise GenerationError(msg)
    return int(match.group(1))


def generations_dir(data_dir: Path) -> Path:
    return data_dir / GENERATIONS_SUBDIR


def _path(directory: Path, generation_id: str) -> Path:
    _number(generation_id)  # never a path outside the directory
    return directory / f"{generation_id}.json"


def read(directory: Path, generation_id: str) -> Generation:
    path = _path(directory, generation_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        generation = Generation(id=generation_id, factory=data["factory"], projects=dict(data.get("projects", {})))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        msg = f"cannot read generation {generation_id} at {path}: {exc}"
        raise GenerationError(msg) from exc
    return generation


def write(directory: Path, generation: Generation) -> None:
    """Write the record atomically, so a reader never sees half of it."""
    directory.mkdir(parents=True, exist_ok=True)
    path = _path(directory, generation.id)
    tmp = path.with_name(f".{path.name}.tmp")
    body = {k: v for k, v in asdict(generation).items() if k != "id"}
    tmp.write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8", newline="\n")
    tmp.replace(path)


def all_generations(directory: Path) -> list[Generation]:
    """Every readable generation, oldest first; a file that is not a generation record is skipped."""
    found: list[Generation] = []
    for path in directory.glob("*.json"):
        if _ID.match(path.stem) is None:
            continue
        try:
            found.append(read(directory, path.stem))
        except GenerationError:
            continue
    return sorted(found, key=lambda g: g.number)


def newest(directory: Path) -> Generation | None:
    found = all_generations(directory)
    return found[-1] if found else None


def create(directory: Path, factory: str) -> Generation:
    """Record a new generation of `factory`, newer than every other; its projects are recorded by its worker."""
    latest = newest(directory)
    number = 1 if latest is None else latest.number + 1
    generation = Generation(id=f"{number}-{factory[:SHORT_SHA]}", factory=factory)
    directory.mkdir(parents=True, exist_ok=True)
    _starting_path(directory, generation.id).touch()  # first, so the record is never seen as started too early
    write(directory, generation)
    return generation


def _starting_path(directory: Path, generation_id: str) -> Path:
    _number(generation_id)
    return directory / f"{generation_id}{STARTING_SUFFIX}"


def mark_started(directory: Path, generation_id: str) -> None:
    """Record that the generation's worker got through its start (its versions made current, when it is the newest)."""
    _starting_path(directory, generation_id).unlink(missing_ok=True)


def started(directory: Path, generation_id: str) -> bool:
    return not _starting_path(directory, generation_id).exists()


def delete(directory: Path, generation_id: str) -> None:
    """Forget a generation whose worker never started, so the one before it is the newest again."""
    _path(directory, generation_id).unlink(missing_ok=True)
    _starting_path(directory, generation_id).unlink(missing_ok=True)
