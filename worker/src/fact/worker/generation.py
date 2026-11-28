"""
The projects a worker serves: every registered project at its head, or a worker generation's recorded home SHAs.

`python -m fact.worker --generation <id>` serves generation `<id>` (`fact.core.generations`): each project is loaded
at the home SHA the record holds. A project the record does not hold yet is loaded at its head and recorded, but only
by the newest generation; an older one skips it, since it never had it. Only the newest generation makes its versions
current, so an older one started again (after a reboot, say) serves its pinned runs and takes no new ones.
A project version that fails to load (its workflows, its config or its sandbox images) is refused: logged, skipped and,
once the worker reaches the API, reported in an `error` notification; every other project keeps running.
"""

import hashlib
from dataclasses import dataclass, field, replace
from pathlib import Path

import httpx
from fact.core import generations
from fact.core.enums import NotificationKind
from fact.core.generations import Generation
from fact.core.logging import get_logger
from fact.core.settings import Settings
from fact.worker.deployments import set_current_version
from fact.worker.loader import ProjectWorkflows, WorkflowLoadError, factory_sha, load_project
from fact.worker.runners import LocalRunner, RunnerError
from temporalio.client import Client

log = get_logger(__name__)


class DuplicateVersionError(RuntimeError):
    """Two registered projects share a build id: the process polls for one, so the other's runs would stall."""


class GenerationMismatchError(RuntimeError):
    """The worker runs from another factory checkout than its generation's pin."""


@dataclass(frozen=True)
class Refused:
    project: str
    error: str


@dataclass(frozen=True)
class Served:
    projects: list[ProjectWorkflows]
    current: bool  # makes its versions current: no generation, or the newest one
    refused: list[Refused] = field(default_factory=list)


def _check_duplicates(loaded: list[ProjectWorkflows], project: ProjectWorkflows) -> None:
    clash = next((p for p in loaded if p.version == project.version), None)
    if clash is not None:  # one process polls a queue once per build id
        msg = f"projects {clash.project!r} and {project.project!r} share build id {project.version}: register their home once"
        raise DuplicateVersionError(msg)


async def load_projects(  # noqa: PLR0913 - options are keyword-only
    settings: Settings,
    runner: LocalRunner,
    factory: str,
    pins: dict[str, str] | None = None,
    *,
    only_pinned: bool = False,
    refused: list[Refused] | None = None,
) -> list[ProjectWorkflows]:
    """
    Load every registered project, at `pins[name]` when given, else at its head; a failing one is logged and skipped.

    With `only_pinned`, the projects `pins` does not name are skipped. Each failing one is appended to `refused`.

    Raise `DuplicateVersionError` when two projects share a build id (one home repository at one SHA).
    """
    loaded: list[ProjectWorkflows] = []
    pins = pins or {}
    refs = [ref for ref in settings.projects if not only_pinned or ref.name in pins]
    for ref in refs:
        sha = pins.get(ref.name)
        try:
            project = await load_project(ref.name, ref.home, runner.remote_url, settings.pinned_dir, factory, sha=sha)
        except (WorkflowLoadError, RunnerError) as exc:
            log.error("workflow_load_failed", project=ref.name, error=str(exc))  # noqa: TRY400 - the message is enough
            if refused is not None:
                refused.append(Refused(ref.name, str(exc)))
            continue
        if not project.workflows:
            log.warning("no_workflows", project=ref.name, checkout=str(project.checkout))
            continue
        _check_duplicates(loaded, project)
        loaded.append(project)
    return loaded


async def serve(settings: Settings, runner: LocalRunner, generation_id: str | None) -> Served:
    """Load the projects to serve: at their heads without a generation, else at the generation's home SHAs."""
    factory = await factory_sha()
    refused: list[Refused] = []
    if generation_id is None:
        return Served(await load_projects(settings, runner, factory, refused=refused), current=True, refused=refused)
    directory = generations.generations_dir(settings.data_dir)
    generation = generations.read(directory, generation_id)
    if generation.factory != factory:
        msg = f"generation {generation.id} is factory {generation.factory}, but this worker runs {factory}"
        raise GenerationMismatchError(msg)
    latest = generations.newest(directory)
    current = latest is not None and latest.id == generation.id
    if not current:  # an older generation never had the projects it did not record
        for ref in settings.projects:
            if ref.name not in generation.projects:
                log.info("project_not_in_generation", project=ref.name, generation=generation.id)
    projects = await load_projects(
        settings, runner, factory, generation.projects, only_pinned=not current, refused=refused
    )
    _record(directory, generation, projects)
    log.info("generation_loaded", generation=generation.id, current=current, projects=[p.project for p in projects])
    return Served(projects, current=current, refused=refused)


def _record(directory: Path, generation: Generation, projects: list[ProjectWorkflows]) -> None:
    """Record the home SHA of each project loaded for the first time, so every later start loads the same one."""
    added = {p.project: p.home_sha for p in projects if p.project not in generation.projects}
    if added:
        generations.write(directory, replace(generation, projects={**generation.projects, **added}))


async def make_current(client: Client, served: Served) -> None:
    """Make every served version current, unless an older generation is served: it only serves its pinned runs."""
    if not served.current:
        return
    for project in served.projects:
        await set_current_version(client, project)  # failing here beats runs that never start


async def notify_refused(http: httpx.AsyncClient, api_url: str, refused: list[Refused]) -> None:
    """Best effort: one `error` notification per refused project and error, so a restart does not repeat it."""
    for item in refused:
        key = f"version_refused:{item.project}:{hashlib.sha256(item.error.encode()).hexdigest()[:16]}"
        body = {
            "kind": NotificationKind.ERROR.value,
            "title": f"Project {item.project}: the worker refused its version"[:300],
            "body": item.error,
            "dedupe_key": key,
        }
        try:
            response = await http.post(f"{api_url.rstrip('/')}/api/v1/notifications", json=body)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("refusal_not_notified", project=item.project, error=str(exc))
