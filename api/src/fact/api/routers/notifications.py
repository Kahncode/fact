"""
Notifications: what a person should look at, such as a run waiting at a gate.

Creating one is idempotent on `dedupe_key`: a repeat for the same run state (an activity retry, a replayed step)
returns the notification that already exists, unchanged, with 200 instead of 201.
"""

from datetime import datetime
from typing import Annotated

from fact.api.deps import get_session
from fact.core.enums import NotificationKind
from fact.core.models import Notification, Run, utcnow
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/v1")

Session = Annotated[AsyncSession, Depends(get_session)]
LIST_DEFAULT = 100
LIST_MAX = 500


class NotificationCreate(BaseModel):
    run_id: str | None = None
    kind: NotificationKind
    title: str = Field(min_length=1, max_length=300)
    body: str = ""
    link: str | None = None
    dedupe_key: str = Field(min_length=1, max_length=300)


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: str | None
    kind: str
    title: str
    body: str
    link: str | None
    created_at: datetime
    read_at: datetime | None
    dedupe_key: str


async def _by_key(session: AsyncSession, key: str) -> Notification | None:
    return await session.scalar(select(Notification).where(Notification.dedupe_key == key))


@router.post("/notifications", status_code=status.HTTP_201_CREATED)
async def create_notification(body: NotificationCreate, response: Response, session: Session) -> NotificationOut:
    if (existing := await _by_key(session, body.dedupe_key)) is not None:
        response.status_code = status.HTTP_200_OK
        return NotificationOut.model_validate(existing)
    if body.run_id is not None and await session.get(Run, body.run_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown run: {body.run_id}")
    notification = Notification(**body.model_dump())
    session.add(notification)
    try:
        await session.commit()
    except IntegrityError:  # another request with the same key got there first
        await session.rollback()
        existing = await _by_key(session, body.dedupe_key)
        if existing is None:
            raise
        response.status_code = status.HTTP_200_OK
        return NotificationOut.model_validate(existing)
    return NotificationOut.model_validate(notification)


@router.get("/notifications")
async def list_notifications(
    session: Session, *, unread: bool = False, limit: Annotated[int, Query(ge=1, le=LIST_MAX)] = LIST_DEFAULT
) -> list[NotificationOut]:
    """Newest first, at most `limit`; with `unread=true`, only those not yet read."""
    query = select(Notification).order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit)
    if unread:
        query = query.where(Notification.read_at.is_(None))
    return [NotificationOut.model_validate(n) for n in (await session.scalars(query)).all()]


@router.post("/notifications/{notification_id}/read")
async def mark_read(notification_id: int, session: Session) -> NotificationOut:
    """Mark a notification read; reading it again keeps the first time."""
    notification = await session.get(Notification, notification_id)
    if notification is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown notification: {notification_id}")
    if notification.read_at is None:
        notification.read_at = utcnow()
        await session.commit()
    return NotificationOut.model_validate(notification)
