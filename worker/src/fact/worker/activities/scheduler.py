"""
Scheduler activities, through the API so the scheduler starts runs exactly as `fact run` does.

- `find_ready_tasks`: `GET /scheduler/ready`, the tasks the scheduler may start and the active runs.
- `check_claude_usage`: the usage gate (`fact.worker.usage_gate`), called only when a tick would start a run. It logs a
  full window or unreadable usage and reports the answer to `PUT /scheduler/usage` for `fact scheduler status`.
- `start_scheduled_run`: `POST /runs/start` for one task, marked `scheduled`; returns its workflow id, or "" when the
  API refuses it (the task changed, or another start took it, since the tick read it). Never retried: a start whose
  answer was lost may have started the run.
"""

import httpx
from fact.core.logging import get_logger
from fact.core.scheduler import (
    CHECK_CLAUDE_USAGE,
    FIND_READY_TASKS,
    START_SCHEDULED_RUN,
    ReadyTasks,
    StartScheduledRunInput,
)
from fact.core.usage_gate import GateState, UsageCheck, UsageCheckInput
from fact.worker.usage_gate import UsageGate
from temporalio import activity

log = get_logger(__name__)

API_PREFIX = "/api/v1"
SERVER_ERROR = 500


class SchedulerActivities:
    def __init__(self, api_base_url: str, http_client: httpx.AsyncClient, gate: UsageGate | None = None) -> None:
        self._base = api_base_url.rstrip("/") + API_PREFIX
        self._http = http_client
        self._gate = gate

    @activity.defn(name=FIND_READY_TASKS)
    async def find_ready_tasks(self) -> ReadyTasks:
        response = await self._http.get(f"{self._base}/scheduler/ready")
        response.raise_for_status()
        return ReadyTasks.model_validate(response.json())

    @activity.defn(name=CHECK_CLAUDE_USAGE)
    async def check_claude_usage(self, spec: UsageCheckInput) -> UsageCheck:
        if self._gate is None:
            return UsageCheck(state=GateState.OFF, reason="no usage gate in this worker")
        previous = self._gate.last
        check = await self._gate.check(spec.now)
        if check.state == GateState.UNAVAILABLE:
            log.error("claude_usage_unavailable", error=check.error)
        elif check.state == GateState.FULL:
            log.info("claude_usage_full", windows=check.reason, next_call_at=check.next_call_at)
        if check is not previous:  # nothing new to report on a cached answer
            await self._report(check)
        return check

    async def _report(self, check: UsageCheck) -> None:
        """Best effort: the preview shows a stale answer rather than the tick failing."""
        try:
            response = await self._http.put(f"{self._base}/scheduler/usage", json=check.model_dump(mode="json"))
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("claude_usage_report_failed", error=str(exc))

    @activity.defn(name=START_SCHEDULED_RUN)
    async def start_scheduled_run(self, spec: StartScheduledRunInput) -> str:
        body = {"task_key": spec.task_key, "project": spec.project, "scheduled": True}
        response = await self._http.post(f"{self._base}/runs/start", json=body)
        if response.status_code >= SERVER_ERROR:
            response.raise_for_status()
        if response.is_error:
            log.warning(
                "scheduled_start_refused", task_key=spec.task_key, project=spec.project, status=response.status_code
            )
            return ""
        workflow_id = str(response.json()["workflow_id"])
        log.info("scheduled_run_started", task_key=spec.task_key, project=spec.project, workflow_id=workflow_id)
        return workflow_id
