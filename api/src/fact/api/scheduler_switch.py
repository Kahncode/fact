"""
Dev only: the scheduler's on/off switch (`fact scheduler on|off`), kept in the database so an API restart keeps it.

One row in `scheduler_switch` (`id` 1); with no row yet the scheduler is on. The dev loop reads it at every tick.
"""

from fact.core.models import SchedulerSwitch, utcnow
from sqlalchemy.ext.asyncio import AsyncSession

SWITCH_ID = 1


async def is_on(session: AsyncSession) -> bool:
    row = await session.get(SchedulerSwitch, SWITCH_ID)
    return row is None or row.on


async def set_on(session: AsyncSession, *, on: bool) -> None:
    await session.merge(SchedulerSwitch(id=SWITCH_ID, on=on, updated_at=utcnow()))
    await session.commit()
