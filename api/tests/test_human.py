"""Gates, steering and the status query through the API, against a time-skipping Temporal running one small gate."""

from collections.abc import AsyncIterator
from datetime import timedelta
from http import HTTPStatus
from typing import Any

import pytest
from fact.core.gates import ANSWER_GATE, STATUS, STEER, GateAnswer, OpenGate, WorkflowStatus
from fastapi import FastAPI
from httpx import AsyncClient
from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

QUEUE = "wf-test"
GATE_ID = "g-1"


@workflow.defn(name="one_gate", sandboxed=False)
class OneGate:
    """Opens gate `g-1`, waits for it, and returns its decision with the steering it was sent."""

    def __init__(self) -> None:
        # Open before any handler runs: an update can arrive in the same activation as the start.
        self._gate: OpenGate | None = OpenGate(
            gate_id=GATE_ID, kind="needs_human", stage_id="needs_human", attempt=1, opened_at=workflow.now()
        )
        self._answer: GateAnswer | None = None
        self._steering: list[str] = []

    @workflow.run
    async def run(self) -> list[str]:
        await workflow.wait_condition(lambda: self._answer is not None)
        self._gate = None
        await workflow.sleep(timedelta(hours=1))  # stay running, so steering and status still reach it
        assert self._answer is not None
        return [self._answer.decision, self._answer.note, *self._steering]

    @workflow.update(name=ANSWER_GATE)
    def answer_gate(self, answer: GateAnswer) -> None:
        self._answer = answer

    @answer_gate.validator
    def _validate(self, answer: GateAnswer) -> None:
        if self._gate is None or answer.gate_id != self._gate.gate_id:
            msg = f"gate {answer.gate_id!r} is not open"
            raise ValueError(msg)

    @workflow.query(name=STATUS)
    def status(self) -> WorkflowStatus:
        return WorkflowStatus(stage="needs_human", attempt=1, gate=self._gate)

    @workflow.signal(name=STEER)
    def steer(self, text: str) -> None:
        self._steering.append(text)


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
    body = {"workflow_id": workflow_id, "project": "fact", "task_key": "task:M9-01", "workflow_type": "one_gate"}
    response = await client.post("/api/v1/runs", json=body)
    assert response.status_code == HTTPStatus.CREATED, response.text
    return response.json()["id"]


async def _touches(client: AsyncClient, run_id: str) -> Any:  # noqa: ANN401
    return (await client.get(f"/api/v1/runs/{run_id}")).json()["human_touches"]


async def test_gate_steer_and_status(app: FastAPI, client: AsyncClient, env: WorkflowEnvironment) -> None:
    app.state.temporal = _Connection(env.client)
    run_id = await _run(client, "run-gate-1")
    async with Worker(env.client, task_queue=QUEUE, workflows=[OneGate]):
        handle = await env.client.start_workflow(OneGate.run, id="run-gate-1", task_queue=QUEUE)

        live = await client.get(f"/api/v1/runs/{run_id}/status")
        assert live.status_code == HTTPStatus.OK, live.text
        assert live.json()["gate"]["gate_id"] == GATE_ID

        wrong = await client.post(f"/api/v1/runs/{run_id}/gates/nope", json={"decision": "approve"})
        assert wrong.status_code == HTTPStatus.CONFLICT
        assert "gate 'nope' is not open" in wrong.json()["detail"]

        url = f"/api/v1/runs/{run_id}/gates/{GATE_ID}"
        first = await client.post(url, json={"decision": "approve", "note": "ship it"})
        assert first.status_code == HTTPStatus.OK, first.text
        assert first.json() == {"run_id": run_id, "human_touches": 1}

        second = await client.post(url, json={"decision": "approve"})
        assert second.status_code == HTTPStatus.CONFLICT
        assert second.json()["detail"] == f"gate '{GATE_ID}' is not open"

        steered = await client.post(f"/api/v1/runs/{run_id}/instructions", json={"text": "use the fixture"})
        assert steered.status_code == HTTPStatus.OK, steered.text
        assert (await client.get(f"/api/v1/runs/{run_id}/status")).json()["gate"] is None

        assert await handle.result() == ["approve", "ship it", "use the fixture"]

    assert await _touches(client, run_id) == 2  # noqa: PLR2004 - one answer, one instruction; refusals don't count


async def test_finished_workflow_is_a_conflict(app: FastAPI, client: AsyncClient, env: WorkflowEnvironment) -> None:
    app.state.temporal = _Connection(env.client)
    run_id = await _run(client, "run-gate-2")
    async with Worker(env.client, task_queue=QUEUE, workflows=[OneGate]):
        handle = await env.client.start_workflow(OneGate.run, id="run-gate-2", task_queue=QUEUE)
        await handle.execute_update(ANSWER_GATE, GateAnswer(gate_id=GATE_ID, decision="reject"))
        await handle.result()

        steered = await client.post(f"/api/v1/runs/{run_id}/instructions", json={"text": "too late"})
        assert steered.status_code == HTTPStatus.CONFLICT, steered.text
        answered = await client.post(f"/api/v1/runs/{run_id}/gates/{GATE_ID}", json={"decision": "approve"})
        assert answered.status_code == HTTPStatus.CONFLICT, answered.text

    assert await _touches(client, run_id) == 0


async def test_unknown_run_and_bad_bodies(client: AsyncClient) -> None:
    missing = await client.post("/api/v1/runs/nope/gates/g-1", json={"decision": "approve"})
    assert missing.status_code == HTTPStatus.NOT_FOUND
    assert (await client.get("/api/v1/runs/nope/status")).status_code == HTTPStatus.NOT_FOUND
    run_id = await _run(client, "run-gate-3")
    bad = await client.post(f"/api/v1/runs/{run_id}/gates/g-1", json={"decision": "maybe"})
    assert bad.status_code == HTTPStatus.UNPROCESSABLE_CONTENT
