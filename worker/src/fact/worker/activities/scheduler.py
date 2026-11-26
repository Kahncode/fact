"""
Scheduler activities, both through the API so the scheduler starts runs exactly as `fact run` does.

- `find_ready_tasks`: `GET /scheduler/ready`, the tasks the scheduler may start and the active runs.
- `start_scheduled_run`: `POST /runs/start` for one task; returns its workflow id, or "" when the API refuses it
  (the task changed, or another start took it, since the tick read it). Never retried: a start whose answer was lost may have started the run.
"""

import httpx
from fact.core.logging import get_logger
from fact.core.scheduler import FIND_READY_TASKS, START_SCHEDULED_RUN, ReadyTasks, StartScheduledRunInput
from temporalio import activity

log = get_logger(__name__)

API_PREFIX = "/api/v1"
SERVER_ERROR = 500


class SchedulerActivities:
    def __init__(self, api_base_url: str, http_client: httpx.AsyncClient) -> None:
        self._base = api_base_url.rstrip("/") + API_PREFIX
        self._http = http_client

    @activity.defn(name=FIND_READY_TASKS)
    async def find_ready_tasks(self) -> ReadyTasks:
        response = await self._http.get(f"{self._base}/scheduler/ready")
        response.raise_for_status()
        return ReadyTasks.model_validate(response.json())

    @activity.defn(name=START_SCHEDULED_RUN)
    async def start_scheduled_run(self, spec: StartScheduledRunInput) -> str:
        body = {"task_key": spec.task_key, "project": spec.project, "only_if_never_run": True}
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
