"""
The person in the loop, through Temporal: answer a gate (update), steer a run (signal), read its live status (query).

Handlers are called by name from `fact.core.gates`; the API never imports `fact.sdk`.
"""

from datetime import timedelta
from typing import Annotated

from fact.api.deps import get_session
from fact.api.temporal import temporal_client
from fact.core.gates import (
    ANSWER_GATE,
    NOTE_MAX_CHARS,
    STATUS,
    STEER,
    STEER_MAX_CHARS,
    GateAnswer,
    GateDecision,
    WorkflowStatus,
)
from fact.core.logging import get_logger
from fact.core.models import Run
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import WorkflowHandle, WorkflowQueryFailedError, WorkflowUpdateFailedError
from temporalio.exceptions import FailureError
from temporalio.service import RPCError, RPCStatusCode

log = get_logger(__name__)
router = APIRouter(prefix="/api/v1")

Session = Annotated[AsyncSession, Depends(get_session)]
# A query or update needs the workflow's worker; without one it would wait for Temporal's own deadline.
# Shorter than the CLI's HTTP timeout, so the person gets this answer rather than a timeout.
WORKER_TIMEOUT = timedelta(seconds=8)


class GateAnswerIn(BaseModel):
    decision: GateDecision
    note: str = Field(default="", max_length=NOTE_MAX_CHARS)


class InstructionIn(BaseModel):
    text: str = Field(min_length=1, max_length=STEER_MAX_CHARS)


class Touched(BaseModel):
    run_id: str
    human_touches: int


async def _handle(request: Request, session: AsyncSession, run_id: str) -> tuple[Run, WorkflowHandle]:
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown run: {run_id}")
    client = await temporal_client(request)
    return run, client.get_workflow_handle(run.workflow_id)


def _rpc_error(run: Run, exc: RPCError) -> HTTPException:
    """Map a Temporal refusal: a finished or unknown workflow is a conflict, anything else is Temporal failing."""
    if exc.status == RPCStatusCode.DEADLINE_EXCEEDED:
        detail = f"No worker answered for {run.workflow_id}: is the project's worker running?"
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail)
    if exc.status == RPCStatusCode.NOT_FOUND:
        return HTTPException(status.HTTP_409_CONFLICT, f"Run {run.workflow_id} is not running in Temporal: {exc}")
    return HTTPException(status.HTTP_502_BAD_GATEWAY, f"Temporal refused the call for {run.workflow_id}: {exc}")


async def _touch(session: AsyncSession, run: Run) -> Touched:
    """Count one human touch atomically: two answers at once must both count."""
    await session.execute(update(Run).where(Run.id == run.id).values(human_touches=Run.human_touches + 1))
    await session.commit()
    await session.refresh(run)
    return Touched(run_id=run.id, human_touches=run.human_touches)


@router.post("/runs/{run_id}/gates/{gate_id}")
async def answer_gate(run_id: str, gate_id: str, body: GateAnswerIn, request: Request, session: Session) -> Touched:
    """Answer an open gate; 409 with the workflow's message when that gate is not open."""
    run, handle = await _handle(request, session, run_id)
    answer = GateAnswer(gate_id=gate_id, decision=body.decision, note=body.note)
    try:
        await handle.execute_update(ANSWER_GATE, answer, rpc_timeout=WORKER_TIMEOUT)
    except WorkflowUpdateFailedError as exc:
        message = exc.cause.message if isinstance(exc.cause, FailureError) else str(exc)
        raise HTTPException(status.HTTP_409_CONFLICT, message) from exc
    except RPCError as exc:
        raise _rpc_error(run, exc) from exc
    log.info("gate_answered", run_id=run.id, gate_id=gate_id, decision=body.decision)
    return await _touch(session, run)


@router.post("/runs/{run_id}/instructions")
async def steer(run_id: str, body: InstructionIn, request: Request, session: Session) -> Touched:
    """Queue instructions for the run's next agent session."""
    run, handle = await _handle(request, session, run_id)
    if not body.text.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Instructions are blank")
    try:
        await handle.signal(STEER, body.text)
    except RPCError as exc:
        raise _rpc_error(run, exc) from exc
    log.info("run_steered", run_id=run.id, chars=len(body.text))
    return await _touch(session, run)


@router.get("/runs/{run_id}/status")
async def live_status(run_id: str, request: Request, session: Session) -> WorkflowStatus:
    """Query the workflow's own view: current stage and attempt, the open gate, outcomes so far."""
    run, handle = await _handle(request, session, run_id)
    try:
        return await handle.query(STATUS, result_type=WorkflowStatus, rpc_timeout=WORKER_TIMEOUT)
    except WorkflowQueryFailedError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Status query failed for {run.workflow_id}: {exc}") from exc
    except RPCError as exc:
        raise _rpc_error(run, exc) from exc
