from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http import HTTPStatus
from typing import Any

from fact.core.models import Event
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

T0 = datetime(2026, 1, 1, tzinfo=UTC)
TWO = 2
RUN = {
    "workflow_id": "wf-1",
    "project": "fact",
    "task_key": "M1-03",
    "workflow_type": "task",
    "complexity": "standard",
    "worker_version": "0.1",
    "home_sha": "abc123",
}


async def _make_run(client: AsyncClient, **overrides: str) -> dict[str, Any]:
    response = await client.post("/api/v1/runs", json={**RUN, **overrides})
    assert response.status_code == HTTPStatus.CREATED, response.text
    return response.json()


async def _make_step(client: AsyncClient, run_id: str, stage: str = "implement", attempt: int = 1) -> dict[str, Any]:
    body = {
        "stage_id": stage,
        "kind": "agent",
        "attempt": attempt,
        "queued_at": T0.isoformat(),
        "started_at": (T0 + timedelta(seconds=1)).isoformat(),
        "harness": "claude",
        "model": "sonnet",
    }
    response = await client.post(f"/api/v1/runs/{run_id}/steps", json=body)
    assert response.status_code == HTTPStatus.CREATED, response.text
    return response.json()


async def _finish(client: AsyncClient, step_id: str, seconds: int, cost: str | None, **extra: object) -> None:
    body = {
        "status": "finished",
        "outcome": "passed",
        "ended_at": (T0 + timedelta(seconds=1 + seconds)).isoformat(),
        "tokens_in": 100,
        "tokens_out": 10,
        "cost_usd": cost,
        **extra,
    }
    response = await client.patch(f"/api/v1/steps/{step_id}", json=body)
    assert response.status_code == HTTPStatus.OK, response.text


async def test_create_and_get_run(client: AsyncClient) -> None:
    run = await _make_run(client)
    assert run["status"] == "queued"
    assert Decimal(run["cost_usd"]) == 0
    detail = (await client.get(f"/api/v1/runs/{run['id']}")).json()
    assert detail["task_key"] == "M1-03"
    assert detail["steps"] == []


async def test_create_run_errors(client: AsyncClient) -> None:
    await _make_run(client)
    duplicate = await client.post("/api/v1/runs", json=RUN)
    assert duplicate.status_code == HTTPStatus.CONFLICT
    unknown = await client.post("/api/v1/runs", json={**RUN, "workflow_id": "wf-2", "project": "nope"})
    assert unknown.status_code == HTTPStatus.NOT_FOUND


async def test_patch_run(client: AsyncClient) -> None:
    run = await _make_run(client)
    response = await client.patch(
        f"/api/v1/runs/{run['id']}",
        json={"status": "finished", "outcome": "merged", "ended_at": T0.isoformat()},
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["status"] == "finished"
    assert response.json()["outcome"] == "merged"
    bad = await client.patch(f"/api/v1/runs/{run['id']}", json={"status": "bogus"})
    assert bad.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


async def test_list_runs_filters(client: AsyncClient) -> None:
    first = await _make_run(client)
    await _make_run(client, workflow_id="wf-2", task_key="M1-04")
    await client.patch(f"/api/v1/runs/{first['id']}", json={"status": "running"})
    assert len((await client.get("/api/v1/runs")).json()) == TWO
    by_status = (await client.get("/api/v1/runs", params={"status": "running"})).json()
    assert [r["id"] for r in by_status] == [first["id"]]
    by_task = (await client.get("/api/v1/runs", params={"task_key": "M1-04"})).json()
    assert [r["workflow_id"] for r in by_task] == ["wf-2"]
    by_workflow = (await client.get("/api/v1/runs", params={"workflow_id": "wf-2"})).json()
    assert [r["task_key"] for r in by_workflow] == ["M1-04"]
    assert (await client.get("/api/v1/runs", params={"project": "remote"})).json() == []
    assert len((await client.get("/api/v1/runs", params={"project": "fact"})).json()) == TWO


async def test_steps_lifecycle_and_order(client: AsyncClient) -> None:
    run = await _make_run(client)
    step = await _make_step(client, run["id"])
    assert step["status"] == "running"
    await _make_step(client, run["id"], stage="tests")
    dup = await client.post(
        f"/api/v1/runs/{run['id']}/steps", json={"stage_id": "implement", "kind": "agent", "attempt": 1}
    )
    assert dup.status_code == HTTPStatus.CONFLICT
    await _finish(client, step["id"], 5, "0.10", log_ref="runs/x/log", outputs={"k": 1})
    detail = (await client.get(f"/api/v1/runs/{run['id']}")).json()
    assert [s["stage_id"] for s in detail["steps"]] == ["implement", "tests"]
    assert detail["steps"][0]["outcome"] == "passed"
    assert detail["steps"][0]["log_ref"] == "runs/x/log"
    assert detail["steps"][0]["outputs"] == {"k": 1}


async def test_totals_after_two_finished_steps(client: AsyncClient) -> None:
    run = await _make_run(client)
    one = await _make_step(client, run["id"], stage="implement")
    two = await _make_step(client, run["id"], stage="review")
    await _finish(client, one["id"], 10, "0.10")
    await _finish(client, two["id"], 20, "0.25", wait_seconds=5)
    detail = (await client.get(f"/api/v1/runs/{run['id']}")).json()
    assert Decimal(detail["cost_usd"]) == Decimal("0.35")
    assert detail["machine_seconds"] == 25  # noqa: PLR2004
    assert detail["human_wait_seconds"] == 5  # noqa: PLR2004
    assert detail["tokens_in"] == 200  # noqa: PLR2004
    assert detail["tokens_out"] == 20  # noqa: PLR2004


async def test_events_batch(client: AsyncClient, app: FastAPI) -> None:
    run = await _make_run(client)
    step = await _make_step(client, run["id"])
    events = [
        {"type": "step_started", "at": T0.isoformat(), "step_id": step["id"], "payload": {"a": 1}},
        {"type": "log", "payload": {"line": "hi"}},
    ]
    response = await client.post(f"/api/v1/runs/{run['id']}/events", json=events)
    assert response.status_code == HTTPStatus.CREATED
    assert response.json() == {"accepted": TWO}
    async with app.state.sessions() as session:
        rows = (await session.scalars(select(Event).order_by(Event.id))).all()
    assert [e.type for e in rows] == ["step_started", "log"]
    assert rows[0].step_id == step["id"]
    assert rows[1].at is not None


async def test_unknown_ids_return_404(client: AsyncClient) -> None:
    run = await _make_run(client)
    other = await _make_run(client, workflow_id="wf-2")
    foreign = await _make_step(client, other["id"])
    assert (await client.get("/api/v1/runs/nope")).status_code == HTTPStatus.NOT_FOUND
    assert (await client.patch("/api/v1/runs/nope", json={})).status_code == HTTPStatus.NOT_FOUND
    step_body = {"stage_id": "s", "kind": "agent"}
    assert (await client.post("/api/v1/runs/nope/steps", json=step_body)).status_code == HTTPStatus.NOT_FOUND
    assert (await client.patch("/api/v1/steps/nope", json={})).status_code == HTTPStatus.NOT_FOUND
    event = [{"type": "log"}]
    assert (await client.post("/api/v1/runs/nope/events", json=event)).status_code == HTTPStatus.NOT_FOUND
    wrong = await client.post(f"/api/v1/runs/{run['id']}/events", json=[{"type": "log", "step_id": foreign["id"]}])
    assert wrong.status_code == HTTPStatus.NOT_FOUND


async def test_null_in_patch_is_ignored_for_required_fields(client: AsyncClient) -> None:
    run = await _make_run(client)
    step = await _make_step(client, run["id"])
    await _finish(client, step["id"], 5, "0.10")
    response = await client.patch(f"/api/v1/steps/{step['id']}", json={"wait_seconds": None, "tokens_in": None})
    assert response.status_code == HTTPStatus.OK
    assert Decimal(response.json()["cost_usd"]) == Decimal("0.10")
    assert response.json()["tokens_in"] == 100  # noqa: PLR2004
    assert (await client.patch(f"/api/v1/runs/{run['id']}", json={"status": None})).status_code == HTTPStatus.OK


async def test_totals_drop_step_that_leaves_finished(client: AsyncClient) -> None:
    run = await _make_run(client)
    step = await _make_step(client, run["id"])
    await _finish(client, step["id"], 5, "0.10")
    await client.patch(f"/api/v1/steps/{step['id']}", json={"status": "running"})
    detail = (await client.get(f"/api/v1/runs/{run['id']}")).json()
    assert Decimal(detail["cost_usd"]) == 0


async def test_an_unreported_cost_stays_null_and_is_counted_not_guessed(client: AsyncClient) -> None:
    run = await _make_run(client)
    known = await _make_step(client, run["id"], stage="implement")
    cut = await _make_step(client, run["id"], stage="review")
    cache = {"cache_read_tokens": 60, "cache_write_tokens": 30}
    await _finish(client, known["id"], 10, "0.10", **cache)
    await _finish(client, cut["id"], 10, None, usage_partial=True, **cache)
    detail = (await client.get(f"/api/v1/runs/{run['id']}")).json()
    steps = {s["stage_id"]: s for s in detail["steps"]}
    assert steps["review"]["cost_usd"] is None
    assert steps["review"]["usage_partial"] is True
    assert Decimal(detail["cost_usd"]) == Decimal("0.10")
    assert detail["cost_unknown_steps"] == 1
    assert (detail["cache_read_tokens"], detail["cache_write_tokens"]) == (120, 60)


async def test_a_step_without_a_harness_has_no_cost(client: AsyncClient) -> None:
    run = await _make_run(client)
    body = {"stage_id": "checks", "kind": "shell", "started_at": T0.isoformat()}
    step = (await client.post(f"/api/v1/runs/{run['id']}/steps", json=body)).json()
    assert step["cost_usd"] is None
    await client.patch(f"/api/v1/steps/{step['id']}", json={"status": "finished", "cost_usd": None})
    detail = (await client.get(f"/api/v1/runs/{run['id']}")).json()
    assert detail["cost_unknown_steps"] == 0  # only agent steps owe a cost
    assert Decimal(detail["cost_usd"]) == 0
