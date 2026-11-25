"""Shared fixtures for the worker tests: the real API app on a temporary data dir."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fact.api.app import create_app
from fact.core.settings import CONFIG_VAR, REPO_ROOT, Settings, reset_settings
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    config = tmp_path / "fact.yaml"
    config.write_text(
        f"data_dir: {(tmp_path / 'data').as_posix()}\n"
        f"projects:\n  - name: fact\n    home: Kahncode/fact\n    path: {REPO_ROOT.as_posix()}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(CONFIG_VAR, str(config))
    reset_settings()
    yield Settings()
    reset_settings()


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def run_id(client: AsyncClient) -> str:
    body = {"workflow_id": "wf-1", "project": "fact", "task_key": "task:M1-05", "workflow_type": "standard"}
    return (await client.post("/api/v1/runs", json=body)).json()["id"]
