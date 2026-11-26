"""
`write_handoff`: store a dropped run's handoff note in the artifact store, through the API, as `handoff_key(run_id)`.

Idempotent: the key is the run's, so the control queue's retry policy may write it again.
"""

import httpx
from fact.core.activity_contract import WRITE_HANDOFF, HandoffInput
from fact.core.artifacts import handoff_key
from fact.core.logging import bind_run, get_logger
from fact.worker.handoff import render_handoff
from temporalio import activity
from temporalio.exceptions import ApplicationError

log = get_logger(__name__)

API_PREFIX = "/api/v1"
SERVER_ERROR = 500


class HandoffActivities:
    def __init__(self, api_base_url: str, http_client: httpx.AsyncClient) -> None:
        self._base = api_base_url.rstrip("/") + API_PREFIX
        self._http = http_client

    @activity.defn(name=WRITE_HANDOFF)
    async def write_handoff(self, spec: HandoffInput) -> str:
        """Write the note and return its key."""
        key = handoff_key(spec.run_id)
        with bind_run(spec.run_id):
            response = await self._http.put(f"{self._base}/artifacts/{key}", content=render_handoff(spec).encode())
            if response.status_code >= SERVER_ERROR:
                response.raise_for_status()  # left to the retry policy
            if response.is_error:
                msg = f"artifacts API refused {key}: {response.status_code} {response.text}"
                raise ApplicationError(msg, non_retryable=True)
            log.info("handoff_written", key=key, steps=len(spec.steps), prs=len(spec.prs))
        return key
