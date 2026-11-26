"""`fact inbox`: a run waiting at a gate shows up there once, end to end through the real activities and the API."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from api_bridge import TemporalStub, fact, serve_api
from fact.core.activity_contract import CONTROL_QUEUE
from fact.core.enums import RunOutcome
from fact.core.settings import CONFIG_VAR, Settings, reset_settings
from fact.core.types import RunInput, RunResult, Task
from fact.sdk.workflow import FactWorkflow
from fact.worker.activities.control import ControlActivities
from fact.worker.activities.gates import GateActivities
from fact.worker.activities.notify import NotifyActivities
from fastapi import FastAPI
from temporalio import workflow
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

QUEUE = "wf-acme"
WF_ID = "run-task-M9-02-1"
BASE = "http://test"
WAIT_S = 20.0
POLL_S = 0.05


@workflow.defn(name="gated", sandboxed=False)
class Gated(FactWorkflow):
    """Opens one `needs_human` gate through `FactWorkflow.gate`, then ends with the decision."""

    @workflow.run
    async def run(self, run_input: RunInput) -> RunResult:
        return await self.execute(run_input, self._body)

    async def _body(self) -> RunResult:
        answer = await self.gate("checks-1", "needs_human", {"summary": "test still failing"})
        return RunResult(outcome=RunOutcome.NEEDS_HUMAN, summary=answer.decision)


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    config = tmp_path / "fact.yaml"
    config.write_text(
        f"data_dir: {(tmp_path / 'data').as_posix()}\nprojects:\n  - name: acme\n    home: acme/home\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(CONFIG_VAR, str(config))
    reset_settings()
    yield Settings()
    reset_settings()


@pytest.fixture
async def api(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FastAPI]:
    async with serve_api(settings, monkeypatch) as application:
        yield application


async def _until_waiting(client: httpx.AsyncClient, run_id: str) -> None:
    """Wait until the run is `waiting` and its gate notification exists: both follow the gate opening."""
    async with asyncio.timeout(WAIT_S):
        while True:
            run = (await client.get(f"/api/v1/runs/{run_id}")).json()
            if run["status"] == "waiting" and (await client.get("/api/v1/notifications")).json():
                return
            await asyncio.sleep(POLL_S)


async def test_a_run_waiting_at_a_gate_shows_up_once_in_the_inbox(api: FastAPI) -> None:
    async with (
        await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter) as env,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url=BASE) as client,
    ):
        api.state.temporal = TemporalStub(env.client)
        control = ControlActivities(BASE, client)
        gates = GateActivities(BASE, client)
        notify = NotifyActivities(BASE, client)
        activities = [
            control.record_run_started,
            control.record_run_finished,
            gates.record_gate_opened,
            gates.record_gate_answered,
            notify.notify,
        ]
        body = {"workflow_id": WF_ID, "project": "acme", "task_key": "task:M9-02", "workflow_type": "gated"}
        run_id = (await client.post("/api/v1/runs", json=body)).json()["id"]
        task = Task(key="task:M9-02", id="M9-02", title="Gate it")
        run_input = RunInput(run_id=run_id, project="acme", task=task, run_url="http://ui.test/run")
        async with (
            Worker(env.client, task_queue=QUEUE, workflows=[Gated]),
            Worker(env.client, task_queue=CONTROL_QUEUE, activities=activities),
        ):
            handle = await env.client.start_workflow(Gated.run, run_input, id=WF_ID, task_queue=QUEUE)
            await _until_waiting(client, run_id)

            inbox = await fact("inbox")
            assert inbox.exit_code == 0, inbox.output
            assert inbox.output.count("gate_waiting") == 1
            assert "task:M9-02 waits for you at gate checks-1 (needs_human)" in inbox.output
            assert "http://ui.test/run" in inbox.output

            answered = await fact("gate", WF_ID, "reject")
            assert answered.exit_code == 0, answered.output
            result = await handle.result(rpc_timeout=None)
        assert result.outcome == RunOutcome.NEEDS_HUMAN

        (notification,) = (await client.get("/api/v1/notifications")).json()
        assert notification["dedupe_key"] == f"{run_id}:gate:checks-1"
        read = await fact("inbox", "read", str(notification["id"]))
        assert read.exit_code == 0, read.output
        assert (await fact("inbox")).output.strip() == "No unread notifications."
        everything = (await fact("inbox", "--all")).output
        assert everything.count("gate_waiting") == 1
        assert " read " in everything
