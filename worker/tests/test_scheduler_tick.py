"""`SchedulerTick` with stub activities (order, caps, failed starts), and `start_scheduled_run` against a mock API."""

import json
import uuid

import httpx
import pytest
from fact.core.activity_contract import CONTROL_QUEUE
from fact.core.scheduler import (
    FIND_READY_TASKS,
    START_SCHEDULED_RUN,
    ProjectLoad,
    ReadyTask,
    ReadyTasks,
    StartScheduledRunInput,
    TickResult,
)
from fact.worker.activities.scheduler import SchedulerActivities
from fact.worker.scheduler import SchedulerTick
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker


def _task(task_id: str, depth: int = 0, project: str = "acme") -> ReadyTask:
    return ReadyTask(project=project, task_key=f"task:{task_id}", task_id=task_id, depth=depth)


READY = ReadyTasks(
    tasks=[_task("M1-05", depth=1), _task("M1-04"), _task("M1-02"), _task("M1-03"), _task("M1-01", project="full")],
    projects=[ProjectLoad(project="acme", active=0, max_runs=5), ProjectLoad(project="full", active=2, max_runs=2)],
    active=2,
    max_runs=6,
)
started: list[str] = []


@activity.defn(name=FIND_READY_TASKS)
async def find_ready_tasks() -> ReadyTasks:
    return READY


@activity.defn(name=START_SCHEDULED_RUN)
async def start_scheduled_run(spec: StartScheduledRunInput) -> str:
    started.append(spec.task_key)
    if spec.task_key == "task:M1-03":
        msg = "API down"
        raise ApplicationError(msg)
    if spec.task_key == "task:M1-04":
        return ""  # refused: the task changed since the tick read it
    return f"run-{spec.task_key}"


async def test_tick_starts_in_order_within_the_caps() -> None:
    started.clear()
    env_cm = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    async with (
        env_cm as env,
        Worker(
            env.client,
            task_queue=CONTROL_QUEUE,
            workflows=[SchedulerTick],
            activities=[find_ready_tasks, start_scheduled_run],
        ),
    ):
        result = await env.client.execute_workflow(
            SchedulerTick.run, id=f"tick-{uuid.uuid4()}", task_queue=CONTROL_QUEUE, result_type=TickResult
        )
    # Four free slots overall; `full` is at its cap; M1-05 is deeper; M1-03 is tried once, never retried.
    assert started == ["task:M1-02", "task:M1-03", "task:M1-04", "task:M1-05"]
    assert result.started == ["run-task:M1-02", "run-task:M1-05"]
    assert result.failed == ["task:M1-03", "task:M1-04"]


@pytest.mark.parametrize(
    ("status", "expected"),
    [(201, "run-task-M1-02-1"), (409, ""), (404, "")],
)
async def test_start_scheduled_run_answers(status: int, expected: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/runs/start"
        assert json.loads(request.content) == {"task_key": "task:M1-02", "project": "acme", "only_if_never_run": True}
        return httpx.Response(status, json={"workflow_id": "run-task-M1-02-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        acts = SchedulerActivities("http://api", http)
        got = await ActivityEnvironment().run(
            acts.start_scheduled_run, StartScheduledRunInput(project="acme", task_key="task:M1-02")
        )
    assert got == expected


async def test_start_scheduled_run_server_error_raises() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(502))) as http:
        acts = SchedulerActivities("http://api", http)
        with pytest.raises(httpx.HTTPStatusError):
            await ActivityEnvironment().run(
                acts.start_scheduled_run, StartScheduledRunInput(project="acme", task_key="task:M1-02")
            )
