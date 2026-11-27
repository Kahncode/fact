"""
The retired `fact-scheduler` Temporal Schedule: deleted at worker start so a dev Temporal database stops ticking.

The scheduler's tick runs in the API (`fact.api.scheduler.run_tick`); Temporal runs only project workflows.
"""

from fact.core.logging import get_logger
from fact.core.scheduler import SCHEDULE_ID
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode


async def delete_leftover_schedule(client: Client) -> bool:
    """Delete the `fact-scheduler` Schedule if one is left; return whether one was."""
    try:
        await client.get_schedule_handle(SCHEDULE_ID).delete()
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        return False
    get_logger("fact.worker").info("leftover_schedule_deleted", schedule_id=SCHEDULE_ID)
    return True
