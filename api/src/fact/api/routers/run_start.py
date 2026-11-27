"""`POST /api/v1/runs/start`: start the project's workflow for a task; `start_task` is also the scheduler's tick."""

from typing import Annotated

from fact.api.deps import get_session
from fact.api.failure_cap import held_runs
from fact.api.run_counts import active_by_project, run_histories
from fact.api.run_start import (
    FoundTask,
    StartError,
    active_run_id,
    find_task,
    latest_handoff,
    next_run_number,
    run_input,
    temporal_link,
    workflow_id,
    workflow_type,
)
from fact.api.task_ref import TaskReadError, project_tasks
from fact.api.temporal import connect_temporal
from fact.core.activity_contract import workflow_queue
from fact.core.artifacts import LocalArtifactStore
from fact.core.enums import RunOutcome, RunStatus, RunTrigger
from fact.core.holds import NEEDS_HUMAN, release_hint
from fact.core.logging import get_logger
from fact.core.models import Project, Run, new_id
from fact.core.project_config import ConfigError
from fact.core.scheduler import run_history_hold
from fact.core.settings import Settings
from fact.core.tasks import is_issue
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import State
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

log = get_logger(__name__)
router = APIRouter(prefix="/api/v1")

Session = Annotated[AsyncSession, Depends(get_session)]
RESERVE_ATTEMPTS = 3


class RunStart(BaseModel):
    task_key: str
    project: str | None = None
    override_deps: bool = False
    scheduled: bool = False  # the scheduler's start: 409 unless the scheduler may still start it (`scheduler_refusal`)


class RunStarted(BaseModel):
    run_id: str
    workflow_id: str
    workflow_type: str
    project: str
    temporal_url: str


async def _find(settings: Settings, body: RunStart) -> FoundTask:
    try:
        found = await find_task(settings, body.task_key, body.project)
    except StartError as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    except ConfigError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except TaskReadError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    ancestor = found.check.gated_by
    if ancestor and not body.override_deps:
        message = f"{body.task_key} is under {ancestor}, which is not marked ready"
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": message, "not_ready_ancestor": ancestor})
    missing = found.check.missing_deps
    if missing and not body.override_deps:
        detail = {"message": f"{body.task_key} has unfinished dependencies", "missing_deps": missing}
        raise HTTPException(status.HTTP_409_CONFLICT, detail)
    return found


async def scheduler_refusal(session: AsyncSession, settings: Settings, found: FoundTask) -> str:
    """
    Why the scheduler may no longer start this task; "" if it may.

    An issue carrying `fact:needs-human`, a run in progress (or, for a file task, ended), the failure cap, or
    `concurrency.max_runs` or the project's `max_runs` reached: the API stays the authority even when the tick's
    snapshot is stale.
    """
    key = found.check.task.key
    if found.check.task.needs_human:
        return f"{key} carries `{NEEDS_HUMAN}`: {release_hint(key)}"
    history = (await run_histories(session, found.ref.name, key)).get((found.ref.name, key), [])
    offered, failed, hold = run_history_hold(history, settings.concurrency.max_failed_runs, labelled=is_issue(key))
    if not offered:
        return f"{key} already has a run"
    if hold and not (is_issue(key) and history[0].run_id in await held_runs(session, [history[0].run_id])):
        return f"{key} failed {failed} runs in a row: start it with `fact run`"  # unless a person removed its label
    active = await active_by_project(session)
    if sum(active.values()) >= settings.concurrency.max_runs:
        return f"max_runs {settings.concurrency.max_runs} reached"
    if active.get(found.ref.name, 0) >= found.config.max_runs:
        return f"project {found.ref.name} max_runs {found.config.max_runs} reached"
    return ""


async def _release(settings: Settings, found: FoundTask) -> None:
    """Remove `fact:needs-human` from an issue a person starts (`fact run`), so its hold is lifted first."""
    key = found.check.task.key
    try:
        async with project_tasks(settings, found.ref) as tasks:
            if tasks is not None:
                await tasks.source.set_labels(key, add=[], remove=[NEEDS_HUMAN])
    except (ConfigError, TaskReadError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"cannot remove {NEEDS_HUMAN} from {key}: {exc}") from exc
    log.info("task_released", task_key=key)


async def _reserve(session: AsyncSession, found: FoundTask, wf_type: str, trigger: RunTrigger) -> Run:
    """Insert the run as `queued` under the next free workflow id; its first activity updates this row."""
    project = await session.scalar(select(Project).where(Project.name == found.ref.name))
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Project {found.ref.name} is not in the database")
    task = found.check.task
    for _ in range(RESERVE_ATTEMPTS):
        wf_id = workflow_id(task.key, await next_run_number(session, task.key))
        run = Run(
            id=new_id(),
            workflow_id=wf_id,
            project_id=project.id,
            task_key=task.key,
            workflow_type=wf_type,
            status=RunStatus.QUEUED,
            complexity=task.complexity,
            trigger=trigger,
        )
        session.add(run)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()  # another start took this number: take the next one
            continue
        return run
    raise HTTPException(status.HTTP_409_CONFLICT, f"Could not reserve a workflow id for {task.key}")


async def _mark_start_failed(session: AsyncSession, run: Run) -> None:
    """
    Keep the row, finished and failed, so its workflow id stays used.

    A start that timed out may still have reached Temporal: its first activity then finds this row and updates it.
    """
    run.status = RunStatus.FINISHED
    run.outcome = RunOutcome.FAILED
    await session.commit()


@router.post("/runs/start", status_code=status.HTTP_201_CREATED)
async def start_run(body: RunStart, request: Request, session: Session) -> RunStarted:
    """Start the task's workflow; after a stopped run, the new run gets its handoff note (`RunInput.handoff_ref`)."""
    return await start_task(request.app.state, body, session)


async def start_task(state: State, body: RunStart, session: AsyncSession) -> RunStarted:
    """`POST /runs/start` without HTTP (the scheduler's tick calls it directly); refusals raise `HTTPException`."""
    settings: Settings = state.settings
    store: LocalArtifactStore = state.artifacts
    found = await _find(settings, body)
    client = await connect_temporal(state.temporal)  # after the checks: a refusal needs no Temporal
    wf_type = workflow_type(found)
    if body.scheduled and (refusal := await scheduler_refusal(session, settings, found)):
        raise HTTPException(status.HTTP_409_CONFLICT, refusal)
    if active := await active_run_id(session, found.ref.name, found.check.task.key):
        detail = f"{found.check.task.key} already has an active run: {active} (stop it first with `fact stop`)"
        raise HTTPException(status.HTTP_409_CONFLICT, detail)
    if not body.scheduled and found.check.task.needs_human:
        await _release(settings, found)
    trigger = RunTrigger.SCHEDULER if body.scheduled else RunTrigger.MANUAL
    for _ in range(RESERVE_ATTEMPTS):
        run = await _reserve(session, found, wf_type, trigger)
        handoff = await latest_handoff(session, store, run.project_id, run.task_key)
        try:
            await client.start_workflow(
                wf_type,
                run_input(found, run.id, temporal_link(settings, run.workflow_id), handoff),
                id=run.workflow_id,
                task_queue=workflow_queue(found.ref.name),
            )
        except WorkflowAlreadyStartedError:
            await _mark_start_failed(session, run)  # the id is taken in Temporal only: keep it used, take the next
            continue
        except Exception as exc:
            await _mark_start_failed(session, run)
            if isinstance(exc, RPCError):
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Temporal refused the start: {exc}") from exc
            raise
        break
    else:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Workflow ids for {found.check.task.key} are taken in Temporal")
    log.info("run_started", run_id=run.id, workflow_id=run.workflow_id, workflow_type=wf_type, handoff=handoff)
    return RunStarted(
        run_id=run.id,
        workflow_id=run.workflow_id,
        workflow_type=wf_type,
        project=found.ref.name,
        temporal_url=temporal_link(settings, run.workflow_id),
    )
