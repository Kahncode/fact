"""Shared fixtures for the worker tests: the real API app on a temporary data dir."""

import shutil
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fact.api.app import create_app
from fact.core import migrate
from fact.core.db import database_url
from fact.core.settings import CONFIG_VAR, REPO_ROOT, Settings, reset_settings
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

# Test modules import shared harnesses (such as `standard_harness`) by name; `--import-mode=importlib` adds no path.
if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))


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


@pytest.fixture(scope="session")
def migrated_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Migrate the schema once; each test starts its app from a copy."""
    config = tmp_path_factory.mktemp("template") / "fact.yaml"
    data_dir = config.parent / "data"
    config.write_text(f"data_dir: {data_dir.as_posix()}\n", encoding="utf-8")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(CONFIG_VAR, str(config))
        reset_settings()
        template = Settings()
        template.db_path.parent.mkdir(parents=True, exist_ok=True)
        migrate.upgrade(database_url(template))
        reset_settings()
    return template.db_path


@pytest.fixture
async def app(settings: Settings, migrated_db: Path) -> AsyncIterator[FastAPI]:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(migrated_db, settings.db_path)
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
