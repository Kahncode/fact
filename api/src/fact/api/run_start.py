"""Start a run: find the task's project, read the task from its default branch, and build the workflow input."""

import asyncio
import re
from dataclasses import dataclass
from urllib.parse import quote

from fact.api.projects import local_path
from fact.api.task_ref import TaskCheck, TaskReadError, fetch, read_task
from fact.core.logging import get_logger
from fact.core.models import Run
from fact.core.project_config import ConfigError, ProjectConfig, load_project_config
from fact.core.settings import ProjectRef, Settings
from fact.core.tasks.files import branch_name
from fact.core.types import RepoCheckout, RunInput, RunLimits
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = get_logger(__name__)
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
SECONDS_PER_MINUTE = 60


class StartError(Exception):
    """A run cannot start; `status` is the HTTP status to answer with."""

    def __init__(self, status: int, detail: str | dict[str, object]) -> None:
        super().__init__(str(detail))
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class FoundTask:
    ref: ProjectRef
    config: ProjectConfig
    check: TaskCheck


def safe_key(task_key: str) -> str:
    """`task:M1-02` becomes `task-M1-02`: only characters safe in a workflow id and a URL."""
    return _UNSAFE.sub("-", task_key).strip("-")


def workflow_id(task_key: str, n: int) -> str:
    return f"run-{safe_key(task_key)}-{n}"


async def next_run_number(session: AsyncSession, task_key: str) -> int:
    """Next `n` for `run-<key>-<n>`: one past the highest used for this task, so numbers are never reused."""
    prefix = f"run-{safe_key(task_key)}-"
    ids = await session.scalars(select(Run.workflow_id).where(Run.workflow_id.startswith(prefix, autoescape=True)))
    used = [int(rest) for wid in ids if (rest := wid.removeprefix(prefix)).isdigit()]
    return max(used, default=0) + 1


def temporal_link(settings: Settings, wf_id: str) -> str:
    ns = quote(settings.temporal.namespace, safe="")
    return f"{settings.temporal.ui_url.rstrip('/')}/namespaces/{ns}/workflows/{quote(wf_id, safe='')}"


def _default_branch(ref: ProjectRef, config: ProjectConfig) -> str:
    home = next((r for r in config.repositories if r.full_name.lower() == ref.home.lower()), config.repositories[0])
    return home.default_branch


def _lookup(ref: ProjectRef, task_key: str) -> FoundTask | None:
    path = local_path(ref)
    if path is None:
        return None
    config = load_project_config(path)
    if config.task_source.kind != "files":
        return None
    branch = _default_branch(ref, config)
    fetch(path, branch)
    check = read_task(path, branch, config.task_source.path, task_key)
    return FoundTask(ref, config, check) if check else None


def _candidates(settings: Settings, project: str | None) -> list[ProjectRef]:
    if project is None:
        return list(settings.projects)
    refs = [r for r in settings.projects if r.name == project]
    if not refs:
        raise StartError(404, f"Unknown project: {project}")
    if refs[0].path is None:
        raise StartError(422, f"Project {project} has no local path in fact.yaml: its task files cannot be read")
    return refs


async def _search(refs: list[ProjectRef], task_key: str, *, strict: bool) -> tuple[list[FoundTask], list[str]]:
    """Look in each project in a thread (git is blocking); unless `strict`, a broken project is skipped, not fatal."""
    found: list[FoundTask] = []
    skipped: list[str] = []
    for ref in refs:
        try:
            f = await asyncio.to_thread(_lookup, ref, task_key)
        except (ConfigError, TaskReadError) as exc:
            if strict:
                raise
            log.warning("task_search_skipped_project", project=ref.name, error=str(exc))
            skipped.append(f"{ref.name}: {exc}")
            continue
        if f is not None:
            found.append(f)
    return found, skipped


async def find_task(settings: Settings, task_key: str, project: str | None) -> FoundTask:
    """Find the project whose task source holds `task_key`: the given project, else every project with a path."""
    found, skipped = await _search(_candidates(settings, project), task_key, strict=project is not None)
    if not found:
        where = f"project {project}" if project else "any project"
        detail = f"Task {task_key} not found on the default branch of {where}"
        if skipped:
            detail += " (skipped: " + "; ".join(skipped) + ")"
        raise StartError(404, detail)
    if len(found) > 1:
        names = ", ".join(f.ref.name for f in found)
        raise StartError(409, f"Task {task_key} is in several projects ({names}): pass project")
    return found[0]


def run_input(settings: Settings, found: FoundTask, run_id: str, run_url: str = "") -> RunInput:
    task = found.check.task
    repos = [
        RepoCheckout(full_name=r.full_name, default_branch=r.default_branch, branch=branch_name(task.key), role=r.role)
        for r in sorted(found.config.repositories, key=lambda r: r.merge_order)
    ]
    defaults = settings.step_defaults
    limits = RunLimits(
        agent_timeout_s=defaults.agent.timeout_minutes * SECONDS_PER_MINUTE,
        shell_timeout_s=defaults.shell.timeout_minutes * SECONDS_PER_MINUTE,
    )
    home = next((r.full_name for r in repos if r.full_name.lower() == found.ref.home.lower()), None)
    return RunInput(
        run_id=run_id,
        project=found.ref.name,
        task=task,
        repos=repos,
        complexity=task.complexity,
        home_repo=home,
        limits=limits,
        run_url=run_url,
    )


def workflow_type(found: FoundTask) -> str:
    workflows = found.config.workflows
    return workflows.by_type.get(found.check.task.type, workflows.default)
