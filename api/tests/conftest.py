"""Shared fixtures for the API tests."""

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
        "projects:\n"
        "  - name: fact\n    home: Kahncode/fact\n"
        f"    path: {REPO_ROOT.as_posix()}\n"
        "  - name: remote\n    home: Kahncode/remote\n",
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
