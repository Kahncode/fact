"""
Subscription usage windows that agent runs report, such as Claude Code's `five_hour` and `seven_day`.

`PUT /usage/{account}` upserts the windows a run saw (by account and window name, the newest report winning);
`GET /usage` lists every account's windows. Only window state is stored: never a token.
"""

from datetime import UTC, datetime
from typing import Annotated

from fact.api.deps import get_session
from fact.core.models import UsageWindow, utcnow
from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/v1")

Session = Annotated[AsyncSession, Depends(get_session)]
Account = Annotated[str, Path(pattern=r"^[a-z0-9_-]{1,50}$")]
MAX_WINDOWS = 20


class WindowIn(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    utilization: float = Field(ge=0)
    resets_at: datetime


class UsageReport(BaseModel):
    windows: list[WindowIn] = Field(max_length=MAX_WINDOWS)


class WindowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account: str
    window: str
    utilization: float
    resets_at: datetime
    updated_at: datetime

    @field_validator("resets_at", "updated_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        """SQLite returns naive datetimes: they are UTC."""
        return value if value.tzinfo else value.replace(tzinfo=UTC)


async def _rows(session: AsyncSession, account: str | None = None) -> list[WindowOut]:
    query = select(UsageWindow).order_by(UsageWindow.account, UsageWindow.window)
    if account is not None:
        query = query.where(UsageWindow.account == account)
    return [WindowOut.model_validate(w) for w in (await session.scalars(query)).all()]


async def _upsert(session: AsyncSession, account: str, windows: list[WindowIn]) -> None:
    existing = {w.window: w for w in await session.scalars(select(UsageWindow).where(UsageWindow.account == account))}
    now = utcnow()
    for window in windows:
        row = existing.get(window.name)
        if row is None:
            row = existing[window.name] = UsageWindow(account=account, window=window.name)
            session.add(row)
        row.utilization, row.resets_at = window.utilization, window.resets_at
        row.updated_at = now
    await session.commit()


@router.put("/usage/{account}")
async def report_usage(account: Account, body: UsageReport, session: Session) -> list[WindowOut]:
    """Record the windows a run saw for `account`; windows it did not mention keep their last state."""
    try:
        await _upsert(session, account, body.windows)
    except IntegrityError:  # another run's report created the same window first: update it instead
        await session.rollback()
        await _upsert(session, account, body.windows)
    return await _rows(session, account)


@router.get("/usage")
async def list_usage(session: Session) -> list[WindowOut]:
    """Every account's windows, by account and window name."""
    return await _rows(session)
