"""`fact stop --force` through the API: the workflow is terminated, and the API records the run and holds its issue."""

from collections.abc import AsyncIterator, Iterator
from http import HTTPStatus
from pathlib import Path

import pytest
from fact.core.enums import RunOutcome, RunStatus
from fact.core.github import app as github_app
from fact.core.models import Run
from fact.core.settings import CONFIG_VAR, Settings, reset_settings
from fastapi import FastAPI
from httpx import AsyncClient
from issues_env import PROJECT, FakeApp, FakeIssues, write_issues_config
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

TASK = "gh:acme/app#12"


class _Connection:
    address = "test"

    def __init__(self, client: Client) -> None:
        self._client = client

    async def client(self) -> Client:
        return self._client


@pytest.fixture(scope="module")
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter) as e:
        yield e


@pytest.fixture
def github() -> FakeIssues:
    return FakeIssues()


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, github: FakeIssues) -> Iterator[Settings]:
    monkeypatch.setenv(CONFIG_VAR, str(write_issues_config(tmp_path)))
    monkeypatch.setattr(github_app, "shared_app", lambda _settings=None: FakeApp(github))
    reset_settings()
    yield Settings()
    reset_settings()


async def _run(client: AsyncClient, workflow_id: str) -> str:
    body = {"workflow_id": workflow_id, "project": PROJECT, "task_key": TASK, "workflow_type": "waits"}
    response = await client.post("/api/v1/runs", json=body)
    assert response.status_code == HTTPStatus.CREATED, response.text
    return response.json()["id"]


async def test_force_stop_terminates_with_no_worker_and_holds_the_issue(
    app: FastAPI, client: AsyncClient, env: WorkflowEnvironment, github: FakeIssues
) -> None:
    app.state.temporal = _Connection(env.client)
    run_id = await _run(client, "run-force-1")
    handle = await env.client.start_workflow("waits", id="run-force-1", task_queue="no-worker-serves-this")
    step = {"stage_id": "implement", "kind": "agent", "attempt": 1, "started_at": "2026-01-01T00:00:00Z"}
    step_id = (await client.post(f"/api/v1/runs/{run_id}/steps", json=step)).json()["id"]

    stopped = await client.post(f"/api/v1/runs/{run_id}/stop", json={"reason": "old version", "force": True})

    assert stopped.status_code == HTTPStatus.ACCEPTED, stopped.text
    assert stopped.json() == {"run_id": run_id, "human_touches": 1, "hold_error": ""}
    assert (await handle.describe()).status == WorkflowExecutionStatus.TERMINATED
    async with app.state.sessions() as session:
        run = await session.get(Run, run_id)
        assert run is not None
        assert (run.status, run.outcome) == (RunStatus.FINISHED, RunOutcome.STOPPED)
        assert run.ended_at is not None
    steps = {s["id"]: s for s in (await client.get(f"/api/v1/runs/{run_id}")).json()["steps"]}
    assert (steps[step_id]["status"], steps[step_id]["outcome"]) == ("finished", "failed_error")
    held = [n for n in (await client.get("/api/v1/notifications")).json() if n["kind"] == "task_held"]
    assert [n["dedupe_key"] for n in held] == [f"{run_id}:held"]
    assert github.labels(12) >= {"fact:needs-human", "fact:ready"}
    assert ("POST", "/repos/acme/app/issues/12/comments") in github.writes


async def test_force_stop_records_a_run_already_gone_from_temporal(
    app: FastAPI, client: AsyncClient, env: WorkflowEnvironment, github: FakeIssues
) -> None:
    """A run terminated by hand in Temporal still shows running: a forced stop records its end."""
    app.state.temporal = _Connection(env.client)
    run_id = await _run(client, "run-force-2")

    stopped = await client.post(f"/api/v1/runs/{run_id}/stop", json={"force": True})

    assert stopped.status_code == HTTPStatus.ACCEPTED, stopped.text
    assert (await client.get(f"/api/v1/runs/{run_id}")).json()["outcome"] == RunOutcome.STOPPED
    assert "fact:needs-human" in github.labels(12)
