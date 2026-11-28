"""
`fact cleanup`'s old versions: the pins, Temporal versions and records of generations nothing needs any more.

The newest generation is current and always kept. An older one is kept while its worker runs or Temporal reports any
of its versions current, ramping or `DRAINING` (runs pinned to it, `fact.cli.drainage`); while Temporal cannot be
asked, every older one is kept. The others are drained: their versions are deleted from Temporal (one Temporal
refuses to delete keeps its generation for the next cleanup), then their generation records, then every pin no kept
generation names. The pin a running `fact dev --in-pin` runs from is kept (`fact.cli.dev_lock.running_pin`). A pin
no generation names at all is kept while it is recent (`fact use` or a worker building it now)
or, for a project pin, while a worker from the source tree runs (it records no generation). `.trash-` folders left by
an earlier cleanup are deleted.
"""

import json
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from fact.cli.dev_lock import running_pin
from fact.cli.drainage import Version
from fact.cli.generations import worker_name
from fact.cli.processes import SOURCE_BUILD, UPGRADE_FILE, ProcessRecord, RunDir, find_process
from fact.cli.removal import Report, delete, is_trash, remove_checkout
from fact.cli.use import FACTORY_SUBDIR, current_file
from fact.core import generations
from fact.core.generations import Generation
from fact.core.settings import Settings

PROJECTS_SUBDIR = "projects"
ORPHAN_GRACE_S = 3600.0  # a pin no generation names yet may be one `fact use` or a starting worker is building
ORPHAN = "no generation uses it"
_SHA = re.compile(r"^[0-9a-f]{40}$")

InUse = Callable[[Sequence[Version]], set[Version] | None]
DeleteVersions = Callable[[Sequence[Version]], dict[Version, str]]


@dataclass(frozen=True)
class State:
    """What decides what stays: the generations, the current pin, the live processes and what Temporal reports."""

    generations: list[Generation]
    current: str | None
    live: dict[str, ProcessRecord]
    in_use: set[Version] | None  # None: Temporal cannot be asked


@dataclass
class Decision:
    remove: list[tuple[Generation, str]] = field(default_factory=list)
    keep: list[tuple[Generation, str]] = field(default_factory=list)


def recorded(run_dir: RunDir) -> dict[str, ProcessRecord | None]:
    """Every process record by name, None for an unreadable one (read only: unlike `RunDir.records`)."""
    found: dict[str, ProcessRecord | None] = {}
    for path in sorted(run_dir.path.glob("*.json")):
        if path.name == UPGRADE_FILE:
            continue
        try:
            found[path.stem] = ProcessRecord(**json.loads(path.read_text(encoding="utf-8")))
        except OSError, ValueError, TypeError:
            found[path.stem] = None
    return found


def live_records(run_dir: RunDir) -> dict[str, ProcessRecord]:
    """Return the recorded processes still alive."""
    return {n: r for n, r in recorded(run_dir).items() if r is not None and find_process(r) is not None}


def current_sha(settings: Settings) -> str | None:
    file = current_file(settings)
    sha = file.read_text(encoding="utf-8").strip() if file.is_file() else ""
    return sha if _SHA.match(sha) else None


def decide(state: State) -> Decision:
    """Split the generations into those to remove (drained) and those to keep, each with why."""
    decision = Decision()
    if not state.generations:
        return decision
    *older, newest = state.generations
    decision.keep.append((newest, "the current generation"))
    for generation in older:
        if worker_name(generation) in state.live:
            decision.keep.append((generation, "its worker is running"))
        elif state.in_use is None:
            decision.keep.append((generation, "Temporal cannot be reached to tell whether runs are pinned to it"))
        elif any(v in state.in_use for v in generation.versions()):
            decision.keep.append((generation, "Temporal reports runs pinned to it (draining)"))
        else:
            decision.remove.append((generation, "drained: no worker running and no open run"))
    return decision


def drop_versions(decision: Decision, delete_versions: DeleteVersions, report: Report) -> None:
    """Delete the drained generations' versions from Temporal; a generation Temporal refuses for moves to kept."""
    versions = [v for g, _ in decision.remove for v in g.versions()]
    errors = {} if report.dry_run else delete_versions(versions)
    remaining: list[tuple[Generation, str]] = []
    for generation, why in decision.remove:
        failed = [f"{d}@{b}: {errors[(d, b)]}" for d, b in generation.versions() if (d, b) in errors]
        if failed:
            report.failures += 1
            decision.keep.append((generation, f"Temporal refused to delete {'; '.join(failed)}; retried next time"))
            continue
        for deployment, build_id in generation.versions():
            report.remove(f"Temporal version {deployment}@{build_id}", f"generation {generation.id} is {why}", _none)
        remaining.append((generation, why))
    decision.remove = remaining


def _none() -> None:
    return None


def _children(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.is_dir()) if folder.is_dir() else []


def _recent(path: Path, now: float) -> bool:
    try:
        return now - path.stat().st_mtime < ORPHAN_GRACE_S
    except OSError:
        return False


@dataclass(frozen=True)
class _Pins:
    """Which pins stay, and the generations being removed, to say why a pin goes."""

    factory: set[str]
    projects: set[tuple[str, str]]
    removed: list[Generation]
    source_worker: bool
    now: float


def _why_removed(pins: _Pins, factory: str | None = None, project: tuple[str, str] | None = None) -> str:
    for generation in pins.removed:
        if generation.factory == factory or (project is not None and project in generation.projects.items()):
            return f"generation {generation.id} is drained"
    return ORPHAN


def _sweep_factory(pins: _Pins, root: Path, report: Report) -> None:
    for path in _children(root):
        label = f"pinned/{FACTORY_SUBDIR}/{path.name}"
        if is_trash(path):
            report.remove(label, "left by an earlier cleanup", lambda p=path: delete(p))
        elif not _SHA.match(path.name):
            continue  # not a pin of ours
        elif path.name in pins.factory:
            report.keep(label, "current, or used by a kept generation, a running process or `fact dev`")
        elif (why := _why_removed(pins, factory=path.name)) == ORPHAN and _recent(path, pins.now):
            report.keep(label, "no generation uses it yet, but it is recent (a pin in progress?)")
        else:
            remove_checkout(report, path, label, why)


def _sweep_projects(pins: _Pins, root: Path, report: Report) -> None:
    for project in _children(root):
        for path in _children(project):
            label = f"pinned/{PROJECTS_SUBDIR}/{project.name}/{path.name}"
            key = (project.name, path.name)
            if is_trash(path):
                report.remove(label, "left by an earlier cleanup", lambda p=path: delete(p))
            elif not _SHA.match(path.name):
                continue  # a clone in progress (`.<sha>.clone`) or not ours
            elif key in pins.projects:
                report.keep(label, "used by a kept generation")
            elif pins.source_worker:
                report.keep(label, "a worker from the source tree is running and may load it")
            elif (why := _why_removed(pins, project=key)) == ORPHAN and _recent(path, pins.now):
                report.keep(label, "no generation uses it yet, but it is recent (a worker starting?)")
            else:
                remove_checkout(report, path, label, why)


def sweep_pins(settings: Settings, state: State, decision: Decision, report: Report, now: float) -> None:
    """Remove every pin and `.trash-` folder under `pinned_dir` that nothing kept needs."""
    kept = [g for g, _ in decision.keep]
    factory = {g.factory for g in kept} | {r.build for r in state.live.values() if r.build != SOURCE_BUILD}
    factory |= {sha for sha in (state.current, running_pin(settings.data_dir)) if sha is not None}
    source_worker = any(r.build == SOURCE_BUILD and r.role.startswith("worker") for r in state.live.values())
    pins = _Pins(
        factory=factory,
        projects={item for g in kept for item in g.projects.items()},
        removed=[g for g, _ in decision.remove],
        source_worker=source_worker,
        now=now,
    )
    _sweep_factory(pins, settings.pinned_dir / FACTORY_SUBDIR, report)
    _sweep_projects(pins, settings.pinned_dir / PROJECTS_SUBDIR, report)


def forget(settings: Settings, removed: Iterable[Generation], report: Report) -> None:
    directory = generations.generations_dir(settings.data_dir)
    for generation in removed:
        report.remove(
            f"generation record {generation.id}",
            "its pins and versions are gone",
            lambda g=generation: generations.delete(directory, g.id),
        )


def clean_versions(  # noqa: PLR0913
    settings: Settings,
    run_dir: RunDir,
    report: Report,
    *,
    in_use: InUse,
    delete_versions: DeleteVersions,
    now: float | None = None,
) -> None:
    """Remove the drained generations' versions, records and pins, and the `.trash-` folders."""
    found = generations.all_generations(generations.generations_dir(settings.data_dir))
    live = live_records(run_dir)
    older_versions = [v for g in found[:-1] for v in g.versions()]
    state = State(found, current_sha(settings), live, in_use(older_versions))
    decision = decide(state)
    for generation, why in decision.keep:
        report.keep(f"generation {generation.id}", why)
    drop_versions(decision, delete_versions, report)
    sweep_pins(settings, state, decision, report, time.time() if now is None else now)
    forget(settings, [g for g, _ in decision.remove], report)
