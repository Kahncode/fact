"""
The scheduler: run a tick, preview the next one, and (dev only) turn the in-process loop on and off.

- `POST /scheduler/tick`: run one tick now and return its `TickResult`, in every environment. Outside dev it is the
  only trigger: an outside cron or a webhook calls it. While the dev loop is off it still ticks (an explicit request).
- `GET /scheduler`: whether this API ticks by itself, whether it is on, and the last tick.
- `GET /scheduler/preview`: what the next tick would start and why the others wait, with the usage gate's last answer.
- `GET /scheduler/active`: runs queued, running or waiting, for the worker's keep-awake.
- `POST /scheduler/on|off` (`dev_router`, mounted only in dev): flip the loop; the state lives in the database
  (`fact.api.scheduler_switch`), so an API restart keeps it, and is on when never set.
"""

from datetime import datetime
from typing import Annotated

from fact.api.deps import get_session
from fact.api.run_counts import active_by_project
from fact.api.scheduler import SchedulerPreview, SchedulerRuntime, find_ready, preview, run_tick
from fact.api.scheduler_switch import is_on, set_on
from fact.core.scheduler import TickResult
from fact.core.settings import Settings
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/v1")
dev_router = APIRouter(prefix="/api/v1")

Session = Annotated[AsyncSession, Depends(get_session)]


class SchedulerState(BaseModel):
    in_process: bool  # dev: this API ticks every `interval_seconds` while `on`; else only `POST /scheduler/tick` ticks
    on: bool | None = None  # dev only: the `fact scheduler on|off` switch; None where there is no switch
    interval_seconds: float | None = None  # dev only
    last_tick_at: datetime | None = None  # since this API started
    last_tick: TickResult | None = None


class ActiveRuns(BaseModel):
    active: int


async def _state(request: Request, session: AsyncSession) -> SchedulerState:
    settings: Settings = request.app.state.settings
    runtime: SchedulerRuntime = request.app.state.scheduler
    dev = settings.is_dev_environment()
    return SchedulerState(
        in_process=dev,
        on=await is_on(session) if dev else None,
        interval_seconds=settings.concurrency.scheduler_interval_seconds if dev else None,
        last_tick_at=runtime.last_tick_at,
        last_tick=runtime.last_tick,
    )


@router.post("/scheduler/tick")
async def scheduler_tick(request: Request) -> TickResult:
    """Run one tick now: start the ready tasks the caps and the usage gate allow; `skipped` if one was running."""
    return await run_tick(request.app.state)


@router.get("/scheduler/preview")
async def scheduler_preview(request: Request, session: Session) -> SchedulerPreview:
    """List the ready tasks in the order the next tick would start them, and why each one waits, if it does."""
    settings: Settings = request.app.state.settings
    runtime: SchedulerRuntime = request.app.state.scheduler
    return preview(await find_ready(settings, session, runtime.listings), runtime.gate.last)


@router.get("/scheduler/active")
async def active_runs(session: Session) -> ActiveRuns:
    return ActiveRuns(active=sum((await active_by_project(session)).values()))


@router.get("/scheduler")
async def scheduler_state(request: Request, session: Session) -> SchedulerState:
    return await _state(request, session)


@dev_router.post("/scheduler/on")
async def scheduler_on(request: Request, session: Session) -> SchedulerState:
    await set_on(session, on=True)
    return await _state(request, session)


@dev_router.post("/scheduler/off")
async def scheduler_off(request: Request, session: Session) -> SchedulerState:
    await set_on(session, on=False)
    return await _state(request, session)
