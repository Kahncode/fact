"""`fact gate`, `fact steer` and the gate in `fact status`: end to end through the API to a time-skipping Temporal."""

from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from api_bridge import TemporalStub, fact, serve_api
from fact.core.gates import ANSWER_GATE, STATUS, STEER, GateAnswer, OpenGate, WorkflowStatus
from fact.core.settings import CONFIG_VAR, Settings, reset_settings
from fastapi import FastAPI
from temporalio import workflow
from temporalio.client import WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import CancelledError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

QUEUE = "wf-acme"
WF_ID = "run-task-M9-01-1"
GATE_ID = "g-1"


@workflow.defn(name="one_gate", sandboxed=False)
class OneGate:
    """Opens gate `g-1` and returns its decision, note and steering once answered."""

    def __init__(self) -> None:
        self._gate: OpenGate | None = None
        self._answer: GateAnswer | None = None
        self._steering: list[str] = []

    @workflow.run
    async def run(self) -> list[str]:
        self._gate = OpenGate(
            gate_id=GATE_ID, kind="needs_human", stage_id="needs_human", attempt=1, opened_at=workflow.now()
        )
        await workflow.wait_condition(lambda: self._answer is not None)
        self._gate = None
        await workflow.sleep(timedelta(hours=1))
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


async def test_gate_moves_the_run_on_and_a_second_answer_is_refused(api: FastAPI) -> None:
    async with (
        await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter) as env,
        Worker(env.client, task_queue=QUEUE, workflows=[OneGate]),
    ):
        api.state.temporal = TemporalStub(env.client)
        body = {"workflow_id": WF_ID, "project": "acme", "task_key": "task:M9-01", "workflow_type": "one_gate"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            assert (await client.post("/api/v1/runs", json=body)).status_code == httpx.codes.CREATED
        handle = await env.client.start_workflow(OneGate.run, id=WF_ID, task_queue=QUEUE)

        shown = await fact("status", WF_ID)
        assert shown.exit_code == 0, shown.output
        assert f"Gate:     {GATE_ID} (needs_human, needs_human #1) open since" in shown.output

        steered = await fact("steer", WF_ID, "use the fixture")
        assert steered.exit_code == 0, steered.output

        first = await fact("gate", WF_ID, "approve", "--note", "ship it")
        assert first.exit_code == 0, first.output
        assert f"Answered gate {GATE_ID} of {WF_ID}: approve" in first.output

        again = await fact("gate", WF_ID, "approve", "--gate", GATE_ID)  # as if it raced the first answer
        assert again.exit_code == 1
        assert f"API answered 409: gate '{GATE_ID}' is not open" in again.output
        no_gate = await fact("gate", WF_ID, "approve")
        assert no_gate.exit_code == 1
        assert f"{WF_ID} has no open gate" in no_gate.output
        assert "Gate:     none open" in (await fact("status", WF_ID)).output

        assert await handle.result() == ["approve", "ship it", "use the fixture"]


async def test_drop_cancels_the_run_with_the_reason(api: FastAPI) -> None:
    async with (
        await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter) as env,
        Worker(env.client, task_queue=QUEUE, workflows=[OneGate]),
    ):
        api.state.temporal = TemporalStub(env.client)
        body = {"workflow_id": WF_ID, "project": "acme", "task_key": "task:M9-01", "workflow_type": "one_gate"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            assert (await client.post("/api/v1/runs", json=body)).status_code == httpx.codes.CREATED
        handle = await env.client.start_workflow(OneGate.run, id=WF_ID, task_queue=QUEUE)

        dropped = await fact("drop", WF_ID, "--reason", "newer workflow")
        assert dropped.exit_code == 0, dropped.output
        assert f"Dropping {WF_ID}" in dropped.output
        with pytest.raises(WorkflowFailureError) as info:
            await handle.result()
        assert isinstance(info.value.cause, CancelledError)
        history = await handle.fetch_history()
        (requested,) = [e for e in history.events if e.HasField("workflow_execution_cancel_requested_event_attributes")]
        assert requested.workflow_execution_cancel_requested_event_attributes.cause == "newer workflow"

    unknown = await fact("drop", "run-nope-1")
    assert unknown.exit_code == 1
    assert "no run with workflow id run-nope-1" in unknown.output
