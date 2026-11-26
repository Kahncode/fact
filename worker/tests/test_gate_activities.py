"""Gate activities against the real runs API: a gate is a step of kind `gate`, and its wait is human wait time."""

from datetime import UTC, datetime, timedelta

import pytest
from fact.core.activity_contract import GateAnsweredInput, GateOpenedInput
from fact.core.enums import StepOutcome
from fact.core.gates import GateDecision
from fact.worker.activities.gates import GateActivities
from httpx import AsyncClient
from temporalio.exceptions import ApplicationError

BASE = "http://test"
OPENED_AT = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
WAIT = timedelta(days=2, hours=3)
DETAILS = {"loop": "checks", "summary": "test still failing"}


def _opened(run_id: str) -> GateOpenedInput:
    return GateOpenedInput(
        run_id=run_id,
        stage_id="needs_human",
        attempt=1,
        gate_id="checks-1",
        kind="needs_human",
        details=DETAILS,
        opened_at=OPENED_AT,
    )


def _answered(run_id: str, step_id: str | None) -> GateAnsweredInput:
    return GateAnsweredInput(
        run_id=run_id,
        step_id=step_id,
        stage_id="needs_human",
        attempt=1,
        gate_id="checks-1",
        kind="needs_human",
        details=DETAILS,
        decision=GateDecision.REJECT,
        note="not worth it",
        outcome=StepOutcome.NEEDS_HUMAN,
        opened_at=OPENED_AT,
        answered_at=OPENED_AT + WAIT,
        wait_seconds=WAIT.total_seconds(),
    )


async def _run(client: AsyncClient, run_id: str) -> dict:
    return (await client.get(f"/api/v1/runs/{run_id}")).json()


async def test_gate_opened_then_answered(client: AsyncClient, run_id: str) -> None:
    gates = GateActivities(BASE, client)

    step_id = await gates.record_gate_opened(_opened(run_id))
    run = await _run(client, run_id)
    assert run["status"] == "waiting"
    (step,) = run["steps"]
    assert (step["id"], step["kind"], step["status"], step["stage_id"]) == (step_id, "gate", "waiting", "needs_human")
    assert step["outputs"] == {"gate_id": "checks-1", "kind": "needs_human", "details": DETAILS}

    await gates.record_gate_answered(_answered(run_id, step_id))
    run = await _run(client, run_id)
    assert run["status"] == "running"
    assert run["human_wait_seconds"] == WAIT.total_seconds()
    assert run["machine_seconds"] == 0  # a gate's wall time is all wait
    (step,) = run["steps"]
    assert (step["status"], step["outcome"], step["wait_seconds"]) == ("finished", "needs_human", WAIT.total_seconds())
    assert step["outputs"]["decision"] == "reject"
    assert step["outputs"]["note"] == "not worth it"


async def test_gate_opened_twice_finds_the_same_step(client: AsyncClient, run_id: str) -> None:
    gates = GateActivities(BASE, client)
    first = await gates.record_gate_opened(_opened(run_id))
    assert await gates.record_gate_opened(_opened(run_id)) == first  # a retry after the step was created
    assert len((await _run(client, run_id))["steps"]) == 1


async def test_gate_answered_without_a_step_creates_it(client: AsyncClient, run_id: str) -> None:
    await GateActivities(BASE, client).record_gate_answered(_answered(run_id, None))
    (step,) = (await _run(client, run_id))["steps"]
    assert (step["kind"], step["status"], step["wait_seconds"]) == ("gate", "finished", WAIT.total_seconds())


async def test_gate_of_unknown_run_is_not_retried(client: AsyncClient) -> None:
    with pytest.raises(ApplicationError, match="404") as info:
        await GateActivities(BASE, client).record_gate_opened(_opened("nope"))
    assert info.value.non_retryable
