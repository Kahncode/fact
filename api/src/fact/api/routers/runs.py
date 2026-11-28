from typing import Annotated, Any

from fact.api.deps import get_session
from fact.api.run_schemas import (
    EventIn,
    EventsAccepted,
    RunCreate,
    RunDetail,
    RunOut,
    RunUpdate,
    StepCreate,
    StepOut,
    StepUpdate,
)
from fact.api.run_totals import close_running_steps, recompute_totals
from fact.core.enums import RunStatus, StepStatus
from fact.core.models import Event, Project, Run, Step
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/v1")

Session = Annotated[AsyncSession, Depends(get_session)]


async def _run_or_404(session: AsyncSession, run_id: str) -> Run:
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown run: {run_id}")
    return run


async def _step_or_404(session: AsyncSession, step_id: str) -> Step:
    step = await session.get(Step, step_id)
    if step is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown step: {step_id}")
    return step


RUN_NULLABLE = {"outcome", "started_at", "ended_at"}
STEP_NULLABLE = {
    "outcome",
    "started_at",
    "ended_at",
    "cost_usd",
    "turns",
    "peak_context_tokens",
    "log_ref",
    "transcript_ref",
    "outputs",
}


def _changes(body: BaseModel, nullable: set[str]) -> dict[str, Any]:
    """Fields the client set; an explicit null is ignored for NOT NULL columns."""
    return {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None or k in nullable}


async def _commit_unique(session: AsyncSession, detail: str) -> None:
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail) from exc


@router.post("/runs", status_code=status.HTTP_201_CREATED)
async def create_run(body: RunCreate, session: Session) -> RunOut:
    project = await session.scalar(select(Project).where(Project.name == body.project))
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown project: {body.project}")
    run = Run(project_id=project.id, **body.model_dump(exclude={"project"}))
    session.add(run)
    await _commit_unique(session, f"Run already exists for workflow_id {body.workflow_id}")
    return RunOut.model_validate(run)


@router.patch("/runs/{run_id}")
async def update_run(run_id: str, body: RunUpdate, session: Session) -> RunOut:
    run = await _run_or_404(session, run_id)
    for field, value in _changes(body, RUN_NULLABLE).items():
        setattr(run, field, value)
    if run.status == RunStatus.FINISHED:
        await close_running_steps(session, run)
    await session.commit()
    return RunOut.model_validate(run)


@router.get("/runs")
async def list_runs(
    session: Session,
    project: str | None = None,
    run_status: Annotated[str | None, Query(alias="status")] = None,
    task_key: str | None = None,
    workflow_id: str | None = None,
) -> list[RunOut]:
    query = select(Run).order_by(Run.created_at.desc(), Run.id)
    if project is not None:
        query = query.join(Project, Project.id == Run.project_id).where(Project.name == project)
    if run_status is not None:
        query = query.where(Run.status == run_status)
    if task_key is not None:
        query = query.where(Run.task_key == task_key)
    if workflow_id is not None:
        query = query.where(Run.workflow_id == workflow_id)
    return [RunOut.model_validate(r) for r in (await session.scalars(query)).all()]


@router.get("/runs/{run_id}")
async def get_run(run_id: str, session: Session) -> RunDetail:
    run = await _run_or_404(session, run_id)
    order = (Step.queued_at, Step.started_at, Step.stage_id, Step.attempt)
    steps = (await session.scalars(select(Step).where(Step.run_id == run_id).order_by(*order))).all()
    return RunDetail(**RunOut.model_validate(run).model_dump(), steps=[StepOut.model_validate(s) for s in steps])


@router.post("/runs/{run_id}/steps", status_code=status.HTTP_201_CREATED)
async def create_step(run_id: str, body: StepCreate, session: Session, response: Response) -> StepOut:
    """
    Create the step (201), or restart the row the same stage and attempt already has (200).

    An activity retried after its worker died posts the same step again: it takes the row over and finishes it.
    """
    run = await _run_or_404(session, run_id)
    existing = await _existing_step(session, run_id, body)
    if existing is None:
        step = Step(run_id=run_id, status=_initial_status(body), **body.model_dump())
        session.add(step)
        try:
            await session.commit()
        except IntegrityError:  # the same step posted at the same moment: take over the row that won
            await session.rollback()
            run = await _run_or_404(session, run_id)  # the rollback expired it
            existing = await _existing_step(session, run_id, body)
            if existing is None:
                raise
        else:
            return StepOut.model_validate(step)
    response.status_code = status.HTTP_200_OK
    for field, value in body.model_dump().items():
        setattr(existing, field, value)
    existing.status, existing.outcome, existing.ended_at = _initial_status(body), None, None
    await session.flush()
    await recompute_totals(session, run)  # a step that was finished leaves the totals until it finishes again
    await session.commit()
    return StepOut.model_validate(existing)


def _initial_status(body: StepCreate) -> StepStatus:
    return StepStatus.RUNNING if body.started_at else StepStatus.QUEUED


async def _existing_step(session: AsyncSession, run_id: str, body: StepCreate) -> Step | None:
    query = select(Step).where(Step.run_id == run_id, Step.stage_id == body.stage_id, Step.attempt == body.attempt)
    return await session.scalar(query)


@router.patch("/steps/{step_id}")
async def update_step(step_id: str, body: StepUpdate, session: Session) -> StepOut:
    step = await _step_or_404(session, step_id)
    for field, value in _changes(body, STEP_NULLABLE).items():
        setattr(step, field, value)
    await session.flush()
    # Always recompute: a step moving out of finished must also leave the totals.
    await recompute_totals(session, await _run_or_404(session, step.run_id))
    await session.commit()
    return StepOut.model_validate(step)


@router.post("/runs/{run_id}/events", status_code=status.HTTP_201_CREATED)
async def add_events(run_id: str, body: list[EventIn], session: Session) -> EventsAccepted:
    await _run_or_404(session, run_id)
    for item in body:
        if item.step_id is not None:
            step = await session.get(Step, item.step_id)
            if step is None or step.run_id != run_id:
                raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown step in this run: {item.step_id}")
        values = item.model_dump(exclude_none=True, exclude={"payload"})
        session.add(Event(run_id=run_id, payload=item.payload, **values))
    await session.commit()
    return EventsAccepted(accepted=len(body))
