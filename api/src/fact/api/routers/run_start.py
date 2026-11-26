"""`POST /api/v1/runs/start`: start the project's workflow for a task."""

from typing import Annotated

from fact.api.deps import get_artifacts, get_session
from fact.api.run_start import (
    FoundTask,
    StartError,
    find_task,
    latest_handoff,
    next_run_number,
    run_input,
    temporal_link,
    workflow_id,
    workflow_type,
)
from fact.api.scheduler import run_histories
from fact.api.task_ref import TaskReadError
from fact.api.temporal import temporal_client
from fact.core.activity_contract import workflow_queue
from fact.core.artifacts import LocalArtifactStore
from fact.core.enums import RunOutcome, RunStatus, RunTrigger
from fact.core.logging import get_logger
from fact.core.models import Project, Run, new_id
from fact.core.project_config import ConfigError
from fact.core.scheduler import run_history_hold
from fact.core.settings import Settings
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

log = get_logger(__name__)
router = APIRouter(prefix="/api/v1")

Session = Annotated[AsyncSession, Depends(get_session)]
Store = Annotated[LocalArtifactStore, Depends(get_artifacts)]
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
    missing = found.check.missing_deps
    if missing and not body.override_deps:
        detail = {"message": f"{body.task_key} has unfinished dependencies", "missing_deps": missing}
        raise HTTPException(status.HTTP_409_CONFLICT, detail)
    return found


async def scheduler_refusal(session: AsyncSession, settings: Settings, found: FoundTask) -> str:
    """Why the scheduler may no longer start this task (a run in progress or ended, or the failure cap); "" if it may."""
    key = found.check.task.key
    history = (await run_histories(session, found.ref.name, key)).get((found.ref.name, key), [])
    offered, failed, hold = run_history_hold(history, settings.concurrency.max_failed_runs)
    if not offered:
        return f"{key} already has a run"
    return f"{key} failed {failed} runs in a row: start it with `fact run`" if hold else ""


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
async def start_run(body: RunStart, request: Request, session: Session, store: Store) -> RunStarted:
    """Start the task's workflow; after a stopped run, the new run gets its handoff note (`RunInput.handoff_ref`)."""
    settings: Settings = request.app.state.settings
    found = await _find(settings, body)
    client = await temporal_client(request)  # after the checks: a refusal needs no Temporal
    wf_type = workflow_type(found)
    if body.scheduled and (refusal := await scheduler_refusal(session, settings, found)):
        raise HTTPException(status.HTTP_409_CONFLICT, refusal)
    trigger = RunTrigger.SCHEDULER if body.scheduled else RunTrigger.MANUAL
    for _ in range(RESERVE_ATTEMPTS):
        run = await _reserve(session, found, wf_type, trigger)
        handoff = await latest_handoff(session, store, run.project_id, run.task_key)
        try:
            await client.start_workflow(
                wf_type,
                run_input(settings, found, run.id, temporal_link(settings, run.workflow_id), handoff),
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
