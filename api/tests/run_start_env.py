"""Shared by the `POST /api/v1/runs/start` tests: the factory config, a fake Temporal and request helpers."""

from http import HTTPStatus
from pathlib import Path
from typing import Any

from fact.core.activity_contract import workflow_queue
from fact.core.types import RunInput
from httpx import AsyncClient
from task_repo import PROJECT, PROJECT_YAML, write
from temporalio.client import Client


def write_config(tmp_path: Path, checkout: Path) -> Path:
    """`acme` reads its tasks from `checkout`; `remote` has no checkout; `broken` has a config but is no git repo."""
    broken = tmp_path / "broken"
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
    return config


class Connection:
    address = "test"

    def __init__(self, client: Client | None) -> None:
        self._client = client

    async def client(self) -> Client:
        if self._client is None:
            msg = "no Temporal in this test"
            raise RuntimeError(msg)
        return self._client


class FailingClient:
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


async def start(client: AsyncClient, **body: Any) -> Any:  # noqa: ANN401
    return await client.post("/api/v1/runs/start", json={"project": PROJECT, **body})


async def finish(client: AsyncClient, run_id: str, outcome: str = "merged") -> None:
    update = {"status": "finished", "outcome": outcome, "started_at": "2026-11-01T00:00:00Z"}
    assert (await client.patch(f"/api/v1/runs/{run_id}", json=update)).status_code == HTTPStatus.OK
