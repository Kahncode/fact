"""`POST /api/v1/runs/start`: task read from origin, dependency refusal, workflow id numbering, and a real start."""

from collections.abc import AsyncIterator, Iterator
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
from fact.api.app import create_app
from fact.api.run_start import FoundTask, next_run_number, run_input, safe_key, workflow_id
from fact.api.task_ref import TaskCheck
from fact.core.activity_contract import workflow_queue
from fact.core.artifacts import handoff_key
from fact.core.project_config import ProjectConfig
from fact.core.settings import CONFIG_VAR, ProjectRef, Settings, reset_settings
from fact.core.types import RunInput, Task
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from task_repo import PROJECT, PROJECT_YAML, write
from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker


@pytest.fixture
def settings(tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    broken = tmp_path / "broken"  # a valid config, but not a git checkout
    write(broken, {".fact/project.yaml": PROJECT_YAML})
    config = tmp_path / "fact.yaml"
    config.write_text(
        f"data_dir: {(tmp_path / 'data').as_posix()}\n"
        "temporal:\n  ui_url: http://ui.test/\n"
        f"projects:\n  - name: {PROJECT}\n    home: acme/home\n    path: {checkout.as_posix()}\n"
        "  - name: remote\n    home: acme/remote\n"
        f"  - name: broken\n    home: acme/home\n    path: {broken.as_posix()}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(CONFIG_VAR, str(config))
    reset_settings()
    yield Settings()
    reset_settings()


class _Connection:
    address = "test"

    def __init__(self, client: Client | None) -> None:
        self._client = client

    async def client(self) -> Client:
        if self._client is None:
            msg = "no Temporal in this test"
            raise RuntimeError(msg)
        return self._client


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        application.state.temporal = _Connection(None)
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@workflow.defn(name="standard", sandboxed=False)
class StubStandard:
    @workflow.run
    async def run(self, run_input: RunInput) -> RunInput:
        return run_input


async def _start(client: AsyncClient, **body: Any) -> Any:  # noqa: ANN401
    return await client.post("/api/v1/runs/start", json={"project": PROJECT, **body})


@pytest.mark.parametrize(
    ("project", "task", "expected"),
    [(True, None, True), (False, None, False), (False, True, True), (True, False, False)],
)
def test_plan_approval_from_task_else_project(project: bool, task: bool | None, expected: bool) -> None:  # noqa: FBT001
    config = ProjectConfig.model_validate(
        {
            "name": PROJECT,
            "repositories": [{"full_name": "acme/home", "role": "backend"}],
            "require_plan_approval": project,
            "workflows": {"default": "standard"},
        }
    )
    found = FoundTask(
        ProjectRef(name=PROJECT, home="acme/home"),
        config,
        TaskCheck(Task(key="task:M1-01", id="M1-01", title="t", require_plan_approval=task), []),
    )
    assert run_input(Settings(), found, "run-1").require_plan_approval is expected


def test_workflow_id_safe_form() -> None:
    assert safe_key("task:M1-02") == "task-M1-02"
    assert safe_key("ENG/12 x") == "ENG-12-x"
    assert workflow_id("task:M1-02", 3) == "run-task-M1-02-3"


async def test_next_run_number_skips_used_numbers(app: FastAPI, client: AsyncClient) -> None:
    for wid in ("run-task-M1-02-1", "run-task-M1-02-5", "run-task-M1-02-x", "run-task-M1-020-9"):
        body = {"workflow_id": wid, "project": PROJECT, "task_key": "task:M1-02", "workflow_type": "standard"}
        assert (await client.post("/api/v1/runs", json=body)).status_code == HTTPStatus.CREATED
    async with app.state.sessions() as session:
        assert await next_run_number(session, "task:M1-02") == 6  # noqa: PLR2004
        assert await next_run_number(session, "task:M1-03") == 1


async def test_refuses_unfinished_dependencies(client: AsyncClient) -> None:
    response = await _start(client, task_key="task:M1-03")
    assert response.status_code == HTTPStatus.CONFLICT, response.text
    assert response.json()["detail"]["missing_deps"] == ["task:M1-02"]
    assert (await client.get("/api/v1/runs")).json() == []


async def test_unknown_task_and_project(client: AsyncClient) -> None:
    response = await _start(client, task_key="task:M1-04")  # only in the working tree, not on origin/main
    assert response.status_code == HTTPStatus.NOT_FOUND, response.text
    response = await _start(client, task_key="task:M1-02", project="nope")
    assert response.status_code == HTTPStatus.NOT_FOUND
    response = await _start(client, task_key="task:M1-02", project="remote")
    assert response.status_code == HTTPStatus.UNPROCESSABLE_CONTENT
    response = await _start(client, task_key="task:M1-02", project="broken")
    assert response.status_code == HTTPStatus.BAD_GATEWAY
    response = await client.post("/api/v1/runs/start", json={"task_key": "task:M9-01"})
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert "skipped: broken" in response.json()["detail"]


class _FailingClient:
    """Raise `errors` in turn from `start_workflow`, then accept."""

    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.ids: list[str] = []
        self.inputs: list[RunInput] = []

    async def start_workflow(self, _type: str, arg: RunInput, *, id: str, task_queue: str) -> None:  # noqa: A002
        assert task_queue == workflow_queue(PROJECT)
        self.ids.append(id)
        self.inputs.append(arg)
        if self.errors:
            raise self.errors.pop(0)


async def test_start_refused_by_temporal_keeps_the_number(app: FastAPI, client: AsyncClient) -> None:
    app.state.temporal = _Connection(_FailingClient(RPCError("nope", RPCStatusCode.UNAVAILABLE, b"")))  # type: ignore[arg-type]
    response = await _start(client, task_key="task:M1-02")
    assert response.status_code == HTTPStatus.BAD_GATEWAY, response.text
    runs = (await client.get("/api/v1/runs")).json()
    assert [(r["workflow_id"], r["status"], r["outcome"]) for r in runs] == [("run-task-M1-02-1", "finished", "failed")]


async def test_id_taken_in_temporal_moves_to_the_next(app: FastAPI, client: AsyncClient) -> None:
    fake = _FailingClient(WorkflowAlreadyStartedError("run-task-M1-02-1", "standard"))
    app.state.temporal = _Connection(fake)  # type: ignore[arg-type]
    response = await _start(client, task_key="task:M1-02")
    assert response.status_code == HTTPStatus.CREATED, response.text
    assert fake.ids == ["run-task-M1-02-1", "run-task-M1-02-2"]
    assert response.json()["workflow_id"] == "run-task-M1-02-2"


async def test_restart_after_a_stop_gets_the_handoff_note(app: FastAPI, client: AsyncClient) -> None:
    fake = _FailingClient()
    app.state.temporal = _Connection(fake)  # type: ignore[arg-type]
    first = (await _start(client, task_key="task:M1-02")).json()
    assert fake.inputs[0].handoff_ref is None
    update = {"status": "finished", "outcome": "stopped", "started_at": "2026-11-01T00:00:00Z"}
    await client.patch(f"/api/v1/runs/{first['run_id']}", json=update)
    app.state.artifacts.put_bytes(handoff_key(first["run_id"]), b"## Handoff\n")

    again = await _start(client, task_key="task:M1-02")
    assert again.status_code == HTTPStatus.CREATED, again.text
    assert fake.inputs[1].handoff_ref == handoff_key(first["run_id"])
    assert fake.inputs[1].repos[0].branch == fake.inputs[0].repos[0].branch  # the same branch, so the same PR


async def test_temporal_down_is_503(client: AsyncClient) -> None:
    response = await _start(client, task_key="task:M1-02")
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE, response.text


async def test_start_runs_the_project_workflow(app: FastAPI, client: AsyncClient) -> None:
    env_cm = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    async with (
        env_cm as env,
        Worker(env.client, task_queue=workflow_queue(PROJECT), workflows=[StubStandard]),
    ):
        app.state.temporal = _Connection(env.client)
        first = await client.post("/api/v1/runs/start", json={"task_key": "task:M1-02"})  # project found by key
        assert first.status_code == HTTPStatus.CREATED, first.text
        started = first.json()
        forced = await _start(client, task_key="task:M1-03", override_deps=True)
        assert forced.status_code == HTTPStatus.CREATED, forced.text
        got: RunInput = await env.client.get_workflow_handle(started["workflow_id"], result_type=RunInput).result()

    assert started["workflow_id"] == "run-task-M1-02-1"
    assert started["workflow_type"] == "standard"
    assert started["project"] == PROJECT
    assert started["temporal_url"] == "http://ui.test/namespaces/default/workflows/run-task-M1-02-1"
    assert got.run_id == started["run_id"]
    assert got.task.key == "task:M1-02"
    assert got.complexity == "complex"
    assert got.home_repo == "acme/home"
    assert got.run_url == started["temporal_url"]
    assert [(r.full_name, r.branch) for r in got.repos] == [("acme/home", "fact/task-m1-02")]
    run = (await client.get(f"/api/v1/runs/{started['run_id']}")).json()
    assert (run["status"], run["workflow_id"], run["task_key"]) == ("queued", "run-task-M1-02-1", "task:M1-02")


async def _finish(client: AsyncClient, run_id: str, outcome: str = "merged") -> None:
    update = {"status": "finished", "outcome": outcome, "started_at": "2026-11-01T00:00:00Z"}
    assert (await client.patch(f"/api/v1/runs/{run_id}", json=update)).status_code == HTTPStatus.OK


async def test_start_refused_while_the_task_has_an_active_run(app: FastAPI, client: AsyncClient) -> None:
    app.state.temporal = _Connection(_FailingClient())  # type: ignore[arg-type]
    first = (await _start(client, task_key="task:M1-02")).json()
    for state in ("queued", "running", "waiting"):
        await client.patch(f"/api/v1/runs/{first['run_id']}", json={"status": state})
        again = await _start(client, task_key="task:M1-02")
        assert again.status_code == HTTPStatus.CONFLICT, again.text
        assert first["run_id"] in again.json()["detail"]
    assert len((await client.get("/api/v1/runs")).json()) == 1


async def test_start_after_every_earlier_run_ended(app: FastAPI, client: AsyncClient) -> None:
    fake = _FailingClient(RPCError("down", RPCStatusCode.UNAVAILABLE, b""))
    app.state.temporal = _Connection(fake)  # type: ignore[arg-type]
    assert (await _start(client, task_key="task:M1-02")).status_code == HTTPStatus.BAD_GATEWAY  # failed start
    first = (await _start(client, task_key="task:M1-02")).json()
    await _finish(client, first["run_id"], "failed")
    second = (await _start(client, task_key="task:M1-02")).json()
    await _finish(client, second["run_id"])
    again = await _start(client, task_key="task:M1-02")
    assert again.status_code == HTTPStatus.CREATED, again.text
    assert again.json()["workflow_id"] == "run-task-M1-02-4"


async def test_scheduler_start_refused_once_the_task_has_a_run(app: FastAPI, client: AsyncClient) -> None:
    fake = _FailingClient(RPCError("down", RPCStatusCode.UNAVAILABLE, b""))
    app.state.temporal = _Connection(fake)  # type: ignore[arg-type]
    assert (await _start(client, task_key="task:M1-02", scheduled=True)).status_code == HTTPStatus.BAD_GATEWAY
    started = await _start(client, task_key="task:M1-02", scheduled=True)  # the failed start does not count
    assert started.status_code == HTTPStatus.CREATED, started.text
    again = await _start(client, task_key="task:M1-02", scheduled=True)
    assert again.status_code == HTTPStatus.CONFLICT, again.text
    active = await _start(client, task_key="task:M1-02")
    assert active.status_code == HTTPStatus.CONFLICT, active.text  # not even by hand while the run is active
    await _finish(client, started.json()["run_id"])
    again = await _start(client, task_key="task:M1-02", scheduled=True)
    assert again.status_code == HTTPStatus.CONFLICT, again.text  # the scheduler never restarts an ended run
    by_hand = await _start(client, task_key="task:M1-02")
    assert by_hand.status_code == HTTPStatus.CREATED  # `fact run` still can
    runs = {r["id"]: r["trigger"] for r in (await client.get("/api/v1/runs")).json()}
    assert runs[started.json()["run_id"]] == "scheduler"
    assert runs[by_hand.json()["run_id"]] == "manual"


@pytest.mark.parametrize(
    ("busy", "reason"),
    [(["remote", "remote"], "max_runs 2 reached"), ([PROJECT], f"project {PROJECT} max_runs 1 reached")],
)
async def test_scheduled_start_refused_at_a_cap(
    app: FastAPI, client: AsyncClient, busy: list[str], reason: str
) -> None:
    app.state.temporal = _Connection(_FailingClient())  # type: ignore[arg-type]
    for n, project in enumerate(busy):  # active runs of other tasks fill concurrency.max_runs or acme's max_runs
        body = {
            "workflow_id": f"busy-{n}",
            "project": project,
            "task_key": f"task:M9-0{n}",
            "workflow_type": "standard",
        }
        assert (await client.post("/api/v1/runs", json=body)).status_code == HTTPStatus.CREATED
    refused = await _start(client, task_key="task:M1-02", scheduled=True)
    assert refused.status_code == HTTPStatus.CONFLICT, refused.text
    assert reason in refused.json()["detail"]
    assert (await _start(client, task_key="task:M1-02")).status_code == HTTPStatus.CREATED  # `fact run` ignores caps
