"""
Gate activities: record a gate as a step of kind `gate` through the runs API.

`record_gate_opened` creates the step, waiting, and moves the run to `waiting`; `record_gate_answered` ends the step
with the decision and its `wait_seconds` (all of its wall time, so none of it counts as machine time) and moves the
run back to `running`. Both are idempotent, so the control queue's retry policy may run them again.
"""

from datetime import datetime
from typing import Any

import httpx
from fact.core.activity_contract import (
    RECORD_GATE_ANSWERED,
    RECORD_GATE_OPENED,
    GateAnsweredInput,
    GateOpenedInput,
)
from fact.core.enums import RunStatus, StepKind, StepStatus
from fact.core.logging import bind_run, get_logger
from temporalio import activity
from temporalio.exceptions import ApplicationError

log = get_logger(__name__)

API_PREFIX = "/api/v1"
SERVER_ERROR = 500


def _refused(what: str, response: httpx.Response) -> ApplicationError:
    """Return a non-retryable error: a 4xx will not get better on retry."""
    msg = f"runs API refused to {what}: {response.status_code} {response.text}"
    return ApplicationError(msg, non_retryable=True)


class GateActivities:
    def __init__(self, api_base_url: str, http_client: httpx.AsyncClient) -> None:
        self._base = api_base_url.rstrip("/") + API_PREFIX
        self._http = http_client

    @activity.defn(name=RECORD_GATE_OPENED)
    async def record_gate_opened(self, spec: GateOpenedInput) -> str:
        """Create the gate's step, waiting, and mark the run waiting; return the step id."""
        with bind_run(spec.run_id):
            step_id = await self._step(spec.run_id, spec.stage_id, spec.attempt, spec.opened_at)
            outputs = {"gate_id": spec.gate_id, "kind": spec.kind, "details": spec.details}
            await self._patch(
                "update the gate step", f"/steps/{step_id}", {"status": StepStatus.WAITING, "outputs": outputs}
            )
            await self._patch("mark the run waiting", f"/runs/{spec.run_id}", {"status": RunStatus.WAITING})
            log.info("gate_opened", gate_id=spec.gate_id, kind=spec.kind, step_id=step_id)
            return step_id

    @activity.defn(name=RECORD_GATE_ANSWERED)
    async def record_gate_answered(self, spec: GateAnsweredInput) -> None:
        """End the gate's step with the decision and the wait, and mark the run running again."""
        with bind_run(spec.run_id):
            step_id = spec.step_id or await self._step(spec.run_id, spec.stage_id, spec.attempt, spec.opened_at)
            body = {
                "status": StepStatus.FINISHED,
                "outcome": spec.outcome,
                "started_at": spec.opened_at.isoformat(),
                "ended_at": spec.answered_at.isoformat(),
                "wait_seconds": spec.wait_seconds,
                "outputs": {
                    "gate_id": spec.gate_id,
                    "kind": spec.kind,
                    "details": spec.details,
                    "decision": spec.decision,
                    "note": spec.note,
                },
            }
            await self._patch("end the gate step", f"/steps/{step_id}", body)
            await self._patch("mark the run running", f"/runs/{spec.run_id}", {"status": RunStatus.RUNNING})
            log.info("gate_answered", gate_id=spec.gate_id, decision=spec.decision, wait_seconds=spec.wait_seconds)

    async def _step(self, run_id: str, stage_id: str, attempt: int, opened_at: datetime) -> str:
        """Create the gate's step; the API returns the row a retry already created."""
        at = opened_at.isoformat()
        body = {"stage_id": stage_id, "kind": StepKind.GATE, "attempt": attempt, "queued_at": at, "started_at": at}
        response = await self._http.post(f"{self._base}/runs/{run_id}/steps", json=body)
        return self._checked("create the gate step", response).json()["id"]

    async def _patch(self, what: str, path: str, body: dict[str, Any]) -> None:
        self._checked(what, await self._http.patch(self._base + path, json=body))

    @staticmethod
    def _checked(what: str, response: httpx.Response) -> httpx.Response:
        """Raise a non-retryable error on a 4xx; a 5xx raises for the activity's retry policy."""
        if response.status_code >= SERVER_ERROR:
            response.raise_for_status()
        if response.is_error:
            raise _refused(what, response)
        return response
