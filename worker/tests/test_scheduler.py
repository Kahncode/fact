"""
The scheduler end to end: ticks through the real API and activities start a task once, within its project's cap.

Also the `fact-scheduler` Schedule: created once, idempotent across worker restarts, its interval following
`fact.yaml`. That part runs on the local dev server: the time-skipping server does not implement Schedules.
"""

import asyncio
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from fact.api.app import create_app
from fact.core.activity_contract import CONTROL_QUEUE, workflow_queue
from fact.core.scheduler import FIND_READY_TASKS, SCHEDULE_ID, ReadyTasks, TickResult
from fact.core.settings import CONFIG_VAR, Settings, reset_settings
from fact.core.types import RunInput
from fact.worker.activities.scheduler import SchedulerActivities
from fact.worker.scheduler import SchedulerTick, ensure_schedule
from fastapi import FastAPI
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

PROJECT = "acme"
PROJECT_YAML = (
    "name: acme\nrepositories:\n  - full_name: acme/home\n    role: backend\nworkflows:\n  default: standard\n"
)
TASK = "---\nid: {id}\ntitle: t\ntype: feature\nstatus: ready\ndepends_on: []\n---\n\n## Goal\n\nDo it.\n"
WAIT_S = 20.0
MERGED = {"status": "finished", "outcome": "merged", "ended_at": "2026-11-26T00:00:00Z"}


def _git(cwd: Path, *args: str) -> None:
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", *ident, *args], cwd=cwd, check=True, capture_output=True)  # noqa: S603, S607


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Project `acme` (cap 1) whose origin/main holds two ready tasks, M1-01 and M1-02."""
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "clone", str(origin), str(work))
    _git(work, "checkout", "-b", "main")
    for rel, text in {
        ".fact/project.yaml": PROJECT_YAML,
        "tasks/M1/01-a.md": TASK.format(id="M1-01"),
        "tasks/M1/02-b.md": TASK.format(id="M1-02"),
    }.items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(text, encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "tasks")
    _git(work, "push", "origin", "main")
    config = tmp_path / "fact.yaml"
    config.write_text(
        f"data_dir: {(tmp_path / 'data').as_posix()}\n"
        f"projects:\n  - name: {PROJECT}\n    home: acme/home\n    path: {work.as_posix()}\n    max_runs: 1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(CONFIG_VAR, str(config))
    reset_settings()
    yield Settings()
    reset_settings()


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


class _Connection:
    address = "test"

    def __init__(self, client: Client) -> None:
        self._client = client

    async def client(self) -> Client:
        return self._client


@workflow.defn(name="standard", sandboxed=False)
class StubStandard:
    @workflow.run
    async def run(self, run_input: RunInput) -> str:
        return run_input.task.key


async def _tick(client: Client) -> TickResult:
    return await client.execute_workflow(
        SchedulerTick.run, id=f"tick-{uuid.uuid4()}", task_queue=CONTROL_QUEUE, result_type=TickResult
    )


async def test_ticks_start_each_task_once_within_the_cap(app: FastAPI) -> None:
    env_cm = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    transport = httpx.ASGITransport(app=app)
    async with (
        env_cm as env,
        httpx.AsyncClient(transport=transport, base_url="http://test") as http,
    ):
        app.state.temporal = _Connection(env.client)
        acts = SchedulerActivities("http://test", http)
        async with (
            Worker(
                env.client,
                task_queue=CONTROL_QUEUE,
                workflows=[SchedulerTick],
                activities=[acts.find_ready_tasks, acts.start_scheduled_run],
            ),
            Worker(env.client, task_queue=workflow_queue(PROJECT), workflows=[StubStandard]),
        ):
            first = await _tick(env.client)
            second = await _tick(env.client)  # M1-01's run is still queued: the cap of 1 is reached
            runs = (await http.get("/api/v1/runs")).json()
            await http.patch(f"/api/v1/runs/{runs[0]['id']}", json=MERGED)
            third = await _tick(env.client)
            fourth = await _tick(env.client)

    assert first.started == ["run-task-M1-01-1"]
    assert second.started == []
    assert third.started == ["run-task-M1-02-1"]  # never M1-01 again
    assert fourth.started == []


async def test_schedule_created_once_and_follows_the_interval(settings: Settings) -> None:
    ticked = asyncio.Event()

    @activity.defn(name=FIND_READY_TASKS)
    async def find_ready_tasks() -> ReadyTasks:
        ticked.set()
        return ReadyTasks()

    async with (
        await WorkflowEnvironment.start_local(data_converter=pydantic_data_converter) as env,
        Worker(env.client, task_queue=CONTROL_QUEUE, workflows=[SchedulerTick], activities=[find_ready_tasks]),
    ):
        assert await ensure_schedule(env.client, settings) == "created"
        handle = env.client.get_schedule_handle(SCHEDULE_ID)
        desc = await handle.describe()
        assert desc.schedule.state.paused  # `scheduler_enabled` is false by default
        assert desc.schedule.spec.intervals[0].every.total_seconds() == settings.concurrency.scheduler_interval_seconds
        await handle.unpause()

        assert await ensure_schedule(env.client, settings) == "unchanged"  # a worker restart
        slower = settings.model_copy(
            update={"concurrency": settings.concurrency.model_copy(update={"scheduler_interval_seconds": 90.0})}
        )
        assert await ensure_schedule(env.client, slower) == "updated"
        desc = await handle.describe()
        assert desc.schedule.spec.intervals[0].every.total_seconds() == 90.0  # noqa: PLR2004
        assert not desc.schedule.state.paused  # a restart never pauses or unpauses it

        await handle.trigger()  # the action starts a real tick on the control queue
        await asyncio.wait_for(ticked.wait(), WAIT_S)
