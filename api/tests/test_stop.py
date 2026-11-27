"""Stopping a run through the API (a cancel with a reason), and finding the handoff note for the task's next run."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from http import HTTPStatus

import pytest
from fact.api.run_start import latest_handoff
from fact.core.artifacts import LocalArtifactStore, handoff_key
from fact.core.enums import RunOutcome, RunStatus
from fact.core.models import Project, Run, new_id
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

QUEUE = "wf-test"
TASK = "task:M9-02"
T0 = datetime(2026, 11, 1, tzinfo=UTC)


@workflow.defn(name="waits", sandboxed=False)
class Waits:
    """Waits until cancelled, then returns the reason it was given, as `FactWorkflow.stop` reads it."""

    @workflow.run
    async def run(self) -> str:
        try:
            await workflow.wait_condition(lambda: False)
        except asyncio.CancelledError:
            return f"stopped: {workflow.cancellation_reason()}"
        return "never"


class _Connection:
    address = "test"

    def __init__(self, client: Client) -> None:
        self._client = client

    async def client(self) -> Client:
        return self._client


@pytest.fixture(scope="module")  # one test server per module: starting one costs about half a second
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter) as e:
        yield e


async def _run(client: AsyncClient, workflow_id: str) -> str:
    body = {"workflow_id": workflow_id, "project": "fact", "task_key": TASK, "workflow_type": "waits"}
    response = await client.post("/api/v1/runs", json=body)
    assert response.status_code == HTTPStatus.CREATED, response.text
    return response.json()["id"]


async def test_stop_cancels_the_workflow_with_the_reason(
    app: FastAPI, client: AsyncClient, env: WorkflowEnvironment
) -> None:
    app.state.temporal = _Connection(env.client)
    run_id = await _run(client, "run-stop-1")
    async with Worker(env.client, task_queue=QUEUE, workflows=[Waits]):
        handle = await env.client.start_workflow(Waits.run, id="run-stop-1", task_queue=QUEUE)
        stopped = await client.post(f"/api/v1/runs/{run_id}/stop", json={"reason": "newer workflow"})
        assert stopped.status_code == HTTPStatus.ACCEPTED, stopped.text
        assert stopped.json() == {"run_id": run_id, "human_touches": 1}
        assert await handle.result() == "stopped: newer workflow"


async def test_stop_refuses_a_finished_or_unknown_run(
    app: FastAPI, client: AsyncClient, env: WorkflowEnvironment
) -> None:
    app.state.temporal = _Connection(env.client)
    assert (await client.post("/api/v1/runs/nope/stop", json={})).status_code == HTTPStatus.NOT_FOUND
    run_id = await _run(client, "run-stop-2")
    await client.patch(f"/api/v1/runs/{run_id}", json={"status": "finished", "outcome": "merged"})
    response = await client.post(f"/api/v1/runs/{run_id}/stop", json={})
    assert response.status_code == HTTPStatus.CONFLICT
    assert "already ended merged" in response.json()["detail"]


async def _add_run(app: FastAPI, outcome: RunOutcome | None, minutes: int, *, started: bool = True) -> str:
    """Add a run of `TASK` created `minutes` after T0, finished unless `outcome` is None; not `started`: a failed start."""
    async with app.state.sessions() as session:
        project = await session.scalar(select(Project).where(Project.name == "fact"))
        assert project is not None
        run = Run(
            id=new_id(),
            workflow_id=f"run-{new_id()}",
            project_id=project.id,
            task_key=TASK,
            workflow_type="standard",
            status=RunStatus.FINISHED if outcome else RunStatus.RUNNING,
            outcome=outcome,
            created_at=T0 + timedelta(minutes=minutes),
            started_at=T0 if started else None,
        )
        session.add(run)
        await session.commit()
        return run.id


async def _latest(app: FastAPI) -> str | None:
    async with app.state.sessions() as session:
        project = await session.scalar(select(Project).where(Project.name == "fact"))
        assert project is not None
        return await latest_handoff(session, app.state.artifacts, project.id, TASK)


def _note(store: LocalArtifactStore, run_id: str) -> str:
    store.put_bytes(handoff_key(run_id), b"## Handoff\n")
    return handoff_key(run_id)


@pytest.mark.usefixtures("client")
async def test_latest_handoff_follows_the_stopped_runs(app: FastAPI) -> None:
    store: LocalArtifactStore = app.state.artifacts
    assert await _latest(app) is None

    older = _note(store, await _add_run(app, RunOutcome.STOPPED, 1))
    assert await _latest(app) == older
    newer = _note(store, await _add_run(app, RunOutcome.STOPPED, 2))
    await _add_run(app, RunOutcome.STOPPED, 3)  # rejected at the plan gate: no note, the search goes on
    await _add_run(app, RunOutcome.FAILED, 4, started=False)  # a failed start is skipped
    await _add_run(app, None, 5)  # still running: not an ended run
    assert await _latest(app) == newer

    await _add_run(app, RunOutcome.MERGED, 6)
    assert await _latest(app) is None  # the task moved on: the note is stale
