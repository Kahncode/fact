"""`POST /api/v1/runs/start`: task read from origin, dependency refusal, workflow id numbering, and a real start."""

import subprocess
from collections.abc import AsyncIterator, Iterator
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
from fact.api.app import create_app
from fact.api.run_start import next_run_number, safe_key, workflow_id
from fact.core.activity_contract import workflow_queue
from fact.core.settings import CONFIG_VAR, Settings, reset_settings
from fact.core.types import RunInput
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

PROJECT = "acme"
PROJECT_YAML = """name: acme
repositories:
  - full_name: acme/home
    role: backend
task_source:
  kind: files
  path: tasks
workflows:
  default: standard
"""


def _task(task_id: str, deps: list[str], status: str = "ready", complexity: str = "standard") -> str:
    return (
        f"---\nid: {task_id}\ntitle: Task {task_id}\ntype: feature\ncomplexity: {complexity}\nstatus: {status}\n"
        f"depends_on: [{', '.join(deps)}]\nhuman_actions: []\n---\n\n## Goal\n\nDo it.\n\n"
        "## Acceptance criteria\n\n- [ ] it works\n"
    )


def _git(cwd: Path, *args: str) -> None:
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", *ident, *args], cwd=cwd, check=True, capture_output=True)  # noqa: S603, S607


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """Clone a bare origin; origin/main holds M1-01 (done), M1-02 (needs M1-01) and M1-03 (needs M1-02)."""
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "clone", str(origin), str(work))
    _git(work, "checkout", "-b", "main")
    _write(
        work,
        {
            ".fact/project.yaml": PROJECT_YAML,
            "tasks/M1/01-one.md": _task("M1-01", [], status="done"),
            "tasks/M1/02-two.md": _task("M1-02", ["M1-01"], complexity="complex"),
            "tasks/M1/03-three.md": _task("M1-03", ["M1-02"]),
        },
    )
    _git(work, "add", ".")
    _git(work, "commit", "-m", "tasks")
    _git(work, "push", "origin", "main")
    # Only in the working tree: a start must not see it.
    _write(work, {"tasks/M1/04-local.md": _task("M1-04", [])})
    return work


@pytest.fixture
def settings(tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    broken = tmp_path / "broken"  # a valid config, but not a git checkout
    _write(broken, {".fact/project.yaml": PROJECT_YAML})
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

    async def start_workflow(self, _type: str, _arg: object, *, id: str, task_queue: str) -> None:  # noqa: A002
        assert task_queue == workflow_queue(PROJECT)
        self.ids.append(id)
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
        again = await _start(client, task_key="task:M1-02")
        assert again.json()["workflow_id"] == "run-task-M1-02-2"
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
