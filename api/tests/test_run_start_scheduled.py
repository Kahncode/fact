"""`POST /api/v1/runs/start` with `scheduled: true`: what the scheduler may start, and its runner pool cap."""

from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path

import pytest
from fact.core.settings import CONFIG_VAR, Settings, reset_settings
from fastapi import FastAPI
from httpx import AsyncClient
from run_start_env import Connection, FailingClient, finish, start, write_config
from task_repo import PROJECT
from temporalio.service import RPCError, RPCStatusCode


@pytest.fixture
def settings(tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    monkeypatch.setenv(CONFIG_VAR, str(write_config(tmp_path, checkout)))
    reset_settings()
    yield Settings()
    reset_settings()


async def test_scheduler_start_refused_once_the_task_has_a_run(app: FastAPI, client: AsyncClient) -> None:
    fake = FailingClient(RPCError("down", RPCStatusCode.UNAVAILABLE, b""))
    app.state.temporal = Connection(fake)  # type: ignore[arg-type]
    assert (await start(client, task_key="task:M1-02", scheduled=True)).status_code == HTTPStatus.BAD_GATEWAY
    started = await start(client, task_key="task:M1-02", scheduled=True)  # the failed start does not count
    assert started.status_code == HTTPStatus.CREATED, started.text
    again = await start(client, task_key="task:M1-02", scheduled=True)
    assert again.status_code == HTTPStatus.CONFLICT, again.text
    active = await start(client, task_key="task:M1-02")
    assert active.status_code == HTTPStatus.CONFLICT, active.text  # not even by hand while the run is active
    await finish(client, started.json()["run_id"])
    again = await start(client, task_key="task:M1-02", scheduled=True)
    assert again.status_code == HTTPStatus.CONFLICT, again.text  # the scheduler never restarts an ended run
    by_hand = await start(client, task_key="task:M1-02")
    assert by_hand.status_code == HTTPStatus.CREATED  # `fact run` still can
    runs = {r["id"]: r["trigger"] for r in (await client.get("/api/v1/runs")).json()}
    assert runs[started.json()["run_id"]] == "scheduler"
    assert runs[by_hand.json()["run_id"]] == "manual"


@pytest.mark.parametrize(
    ("busy", "reason"),
    [([PROJECT], f"project {PROJECT} max_concurrent_runs 1 reached"), (["remote", "remote"], None)],
)
async def test_scheduled_start_refused_only_when_its_pool_is_full(
    app: FastAPI, client: AsyncClient, busy: list[str], reason: str | None
) -> None:
    app.state.temporal = Connection(FailingClient())  # type: ignore[arg-type]
    for n, project in enumerate(busy):  # active runs of other tasks: acme's own fill its pool, remote's do not
        body = {
            "workflow_id": f"busy-{n}",
            "project": project,
            "task_key": f"task:M9-0{n}",
            "workflow_type": "standard",
        }
        assert (await client.post("/api/v1/runs", json=body)).status_code == HTTPStatus.CREATED
    scheduled = await start(client, task_key="task:M1-02", scheduled=True)
    if reason is None:
        assert scheduled.status_code == HTTPStatus.CREATED, scheduled.text
        return
    assert scheduled.status_code == HTTPStatus.CONFLICT, scheduled.text
    assert reason in scheduled.json()["detail"]
    assert (await start(client, task_key="task:M1-02")).status_code == HTTPStatus.CREATED  # `fact run` ignores caps
