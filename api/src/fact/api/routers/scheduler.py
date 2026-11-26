"""
The scheduler: what it may start (`GET /scheduler/ready`, for its tick) and its Schedule's state, paused or not.

The worker creates the `fact-scheduler` Schedule; this only describes, pauses and unpauses it.
"""

from datetime import datetime
from typing import Annotated

from fact.api.deps import get_session
from fact.api.scheduler import find_ready
from fact.api.temporal import temporal_client
from fact.core.scheduler import SCHEDULE_ID, ReadyTasks
from fact.core.settings import Settings
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import ScheduleDescription, ScheduleHandle
from temporalio.service import RPCError, RPCStatusCode

router = APIRouter(prefix="/api/v1")

Session = Annotated[AsyncSession, Depends(get_session)]


class SchedulerState(BaseModel):
    schedule_id: str
    paused: bool
    note: str | None = None
    interval_seconds: float | None = None
    next_ticks: list[datetime] = []
    recent_ticks: list[datetime] = []


def _state(desc: ScheduleDescription) -> SchedulerState:
    intervals = desc.schedule.spec.intervals
    return SchedulerState(
        schedule_id=desc.id,
        paused=desc.schedule.state.paused,
        note=desc.schedule.state.note or None,
        interval_seconds=intervals[0].every.total_seconds() if intervals else None,
        next_ticks=desc.info.next_action_times[:3],
        recent_ticks=[a.started_at for a in desc.info.recent_actions[-3:]],
    )


async def _describe(handle: ScheduleHandle) -> ScheduleDescription:
    try:
        return await handle.describe()
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            detail = f"No Temporal schedule {SCHEDULE_ID}: the worker creates it when it starts"
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail) from exc
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Temporal refused: {exc}") from exc


async def _handle(request: Request) -> ScheduleHandle:
    return (await temporal_client(request)).get_schedule_handle(SCHEDULE_ID)


@router.get("/scheduler/ready")
async def ready(request: Request, session: Session) -> ReadyTasks:
    """Tasks the scheduler may start, read from each project's default branch, and the runs counted against the caps."""
    settings: Settings = request.app.state.settings
    return await find_ready(settings, session)


@router.get("/scheduler")
async def scheduler_state(request: Request) -> SchedulerState:
    return _state(await _describe(await _handle(request)))


async def _set_paused(request: Request, *, paused: bool) -> SchedulerState:
    handle = await _handle(request)
    await _describe(handle)  # 404 before acting on a schedule that is not there
    try:
        if paused:
            await handle.pause(note="paused with `fact scheduler off`")
        else:
            await handle.unpause(note="unpaused with `fact scheduler on`")
    except RPCError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Temporal refused: {exc}") from exc
    return _state(await _describe(handle))


@router.post("/scheduler/on")
async def scheduler_on(request: Request) -> SchedulerState:
    return await _set_paused(request, paused=False)


@router.post("/scheduler/off")
async def scheduler_off(request: Request) -> SchedulerState:
    return await _set_paused(request, paused=True)
