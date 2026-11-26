"""
`notify`: create a notification for a person through the API.

Idempotent: the API keeps one notification per `dedupe_key`, so the control queue's retry policy may run it again.
"""

import httpx
from fact.core.activity_contract import NOTIFY, NotifyInput
from fact.core.logging import bind_run, get_logger
from temporalio import activity
from temporalio.exceptions import ApplicationError

log = get_logger(__name__)

API_PREFIX = "/api/v1"
SERVER_ERROR = 500


class NotifyActivities:
    def __init__(self, api_base_url: str, http_client: httpx.AsyncClient) -> None:
        self._base = api_base_url.rstrip("/") + API_PREFIX
        self._http = http_client

    @activity.defn(name=NOTIFY)
    async def notify(self, spec: NotifyInput) -> int:
        """Create the notification, or find the one with the same `dedupe_key`; return its id."""
        with bind_run(spec.run_id):
            response = await self._http.post(f"{self._base}/notifications", json=spec.model_dump(mode="json"))
            if response.status_code >= SERVER_ERROR:
                response.raise_for_status()  # left to the retry policy
            if response.is_error:  # a 4xx will not get better on retry
                msg = f"notifications API refused {spec.dedupe_key}: {response.status_code} {response.text}"
                raise ApplicationError(msg, non_retryable=True)
            notification_id = int(response.json()["id"])
            log.info("notified", kind=spec.kind, notification_id=notification_id, dedupe_key=spec.dedupe_key)
            return notification_id
