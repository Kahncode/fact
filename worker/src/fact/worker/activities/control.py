"""
Control activities: bookkeeping a workflow needs around its steps.

- `record_run_started` and `record_run_finished`: move the run row through the runs API; the start also records the
  worker version the run is pinned to and its home SHA.
- `record_run_waiting`: mark the run `waiting` while it sleeps without a gate (a usage limit), then `running` again.
"""

from datetime import UTC, datetime
from typing import Any

import httpx
from fact.core.activity_contract import (
    RECORD_RUN_FINISHED,
    RECORD_RUN_STARTED,
    RECORD_RUN_WAITING,
    RunFinishedInput,
    RunStartedInput,
    RunWaitingInput,
)
from fact.core.enums import RunStatus
from fact.core.logging import bind_run, get_logger
from fact.worker.loader import home_sha_of
from temporalio import activity
from temporalio.exceptions import ApplicationError

log = get_logger(__name__)

API_PREFIX = "/api/v1"
SERVER_ERROR = 500


def _non_retryable(message: str) -> ApplicationError:
    return ApplicationError(message, non_retryable=True)


class ControlActivities:
    def __init__(self, api_base_url: str, http_client: httpx.AsyncClient) -> None:
        self._base = api_base_url.rstrip("/") + API_PREFIX
        self._http = http_client

    @activity.defn(name=RECORD_RUN_STARTED)
    async def record_run_started(self, spec: RunStartedInput) -> None:
        body: dict[str, Any] = {"status": RunStatus.RUNNING, "started_at": _now()}
        if spec.worker_version:
            body |= {"worker_version": spec.worker_version, "home_sha": home_sha_of(spec.worker_version)}
        await self._patch_run(spec.run_id, body)

    @activity.defn(name=RECORD_RUN_FINISHED)
    async def record_run_finished(self, spec: RunFinishedInput) -> None:
        body = {"status": RunStatus.FINISHED, "outcome": spec.outcome, "ended_at": _now()}
        await self._patch_run(spec.run_id, body)

    @activity.defn(name=RECORD_RUN_WAITING)
    async def record_run_waiting(self, spec: RunWaitingInput) -> None:
        await self._patch_run(spec.run_id, {"status": RunStatus.WAITING if spec.waiting else RunStatus.RUNNING})
        if spec.reason:
            with bind_run(spec.run_id):
                log.info("run_waiting" if spec.waiting else "run_resumed", reason=spec.reason)

    async def _patch_run(self, run_id: str, body: dict[str, Any]) -> None:
        """PATCH the run; a 4xx will not get better on retry, anything else is left to the activity's retry policy."""
        try:
            response = await self._http.patch(f"{self._base}/runs/{run_id}", json=body)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < SERVER_ERROR:
                msg = f"runs API refused the update of run {run_id}: {exc.response.status_code} {exc.response.text}"
                raise _non_retryable(msg) from exc
            raise
        with bind_run(run_id):
            log.info("run_recorded", **{k: str(v) for k, v in body.items()})


def _now() -> str:
    return datetime.now(UTC).isoformat()
