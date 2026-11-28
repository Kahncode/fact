"""Start a run: find the task's project, read the task from its default branch, and build the workflow input."""

import re
from dataclasses import dataclass
from urllib.parse import quote

from fact.api.task_ref import TaskCheck, TaskReadError, check_task, project_tasks
from fact.core.artifacts import ArtifactStore, handoff_key
from fact.core.enums import RunOutcome, RunStatus
from fact.core.logging import get_logger
from fact.core.models import Project, Run
from fact.core.project_config import ConfigError, ProjectConfig
from fact.core.settings import ProjectRef, Settings
from fact.core.tasks import branch_name
from fact.core.types import RepoCheckout, RunInput
from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

log = get_logger(__name__)
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


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


async def latest_handoff(session: AsyncSession, store: ArtifactStore, project_id: int, task_key: str) -> str | None:
    """
    Key of the newest handoff note left by the task's last runs, if they were stopped; else None.

    Runs that never started (a failed start) are skipped; the first other run back that was not stopped ends the
    search, so a merged or failed run in between means the note is stale. A stopped run with no note (its plan was
    rejected, or writing the note failed) is passed over for the older stopped run's note.
    """
    query = (
        select(Run.id, Run.outcome)
        .where(Run.project_id == project_id, Run.task_key == task_key, Run.status == RunStatus.FINISHED)
        .where(Run.started_at.is_not(None))
        .order_by(Run.created_at.desc(), Run.id.desc())
    )
    for run_id, outcome in (await session.execute(query)).all():
        if outcome != RunOutcome.STOPPED:
            return None
        if store.exists(key := handoff_key(run_id)):
            return key
    return None


ACTIVE_STATUSES = (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING)


async def active_run_id(session: AsyncSession, project: str, task_key: str) -> str | None:
    """Id of the task's active run (queued, running or waiting), if any: a task has at most one at a time."""
    query = (
        select(Run.id)
        .join(Project, Project.id == Run.project_id)
        .where(Project.name == project, Run.task_key == task_key, Run.status.in_(ACTIVE_STATUSES))
        .order_by(Run.created_at.desc(), Run.id.desc())
        .limit(1)
    )
    return await session.scalar(query)


def counts_as_run() -> ColumnElement[bool]:
    """Match the runs that count: all but a start that never reached Temporal (see `_mark_start_failed`)."""
    return or_(Run.status != RunStatus.FINISHED, Run.started_at.is_not(None), Run.ended_at.is_not(None))


def temporal_link(settings: Settings, wf_id: str) -> str:
    ns = quote(settings.temporal.namespace, safe="")
    return f"{settings.temporal.ui_url.rstrip('/')}/namespaces/{ns}/workflows/{quote(wf_id, safe='')}"


async def _lookup(settings: Settings, ref: ProjectRef, task_key: str) -> FoundTask | None:
    async with project_tasks(settings, ref) as tasks:
        if tasks is None:
            return None
        check = await check_task(tasks.source, task_key)
        return FoundTask(ref, tasks.config, check) if check else None


def _candidates(settings: Settings, project: str | None) -> list[ProjectRef]:
    if project is None:
        return list(settings.projects)
    refs = [r for r in settings.projects if r.name == project]
    if not refs:
        raise StartError(404, f"Unknown project: {project}")
    if refs[0].path is None:
        raise StartError(422, f"Project {project} has no local path in fact.yaml: its task files cannot be read")
    return refs


async def _search(
    settings: Settings, refs: list[ProjectRef], task_key: str, *, strict: bool
) -> tuple[list[FoundTask], list[str]]:
    """Look in each project's task source; unless `strict`, a broken project is skipped, not fatal."""
    found: list[FoundTask] = []
    skipped: list[str] = []
    for ref in refs:
        try:
            f = await _lookup(settings, ref, task_key)
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
    found, skipped = await _search(settings, _candidates(settings, project), task_key, strict=project is not None)
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


def run_input(found: FoundTask, run_id: str, run_url: str = "", handoff_ref: str | None = None) -> RunInput:
    task = found.check.task
    repos = [
        RepoCheckout(full_name=r.full_name, default_branch=r.default_branch, branch=branch_name(task.key), role=r.role)
        for r in sorted(found.config.repositories, key=lambda r: r.merge_order)
    ]
    home = next((r.full_name for r in repos if r.full_name.lower() == found.ref.home.lower()), None)
    approval = task.require_plan_approval
    return RunInput(
        run_id=run_id,
        project=found.ref.name,
        task=task,
        repos=repos,
        complexity=task.complexity,
        home_repo=home,
        routing=found.config.routing,
        step_defaults=found.config.step_defaults,
        run_url=run_url,
        require_plan_approval=found.config.require_plan_approval if approval is None else approval,
        handoff_ref=handoff_ref,
        auto_merge=found.config.auto_merge,
        merge_method=found.config.merge_method,
        review_spec_ambiguous=found.config.classify.review_spec_ambiguous,
    )


def workflow_type(found: FoundTask) -> str:
    workflows = found.config.workflows
    return workflows.by_type.get(found.check.task.type, workflows.default)
