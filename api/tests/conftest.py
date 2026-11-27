"""Shared fixtures for the API tests."""

import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fact.api.app import create_app
from fact.core.settings import CONFIG_VAR, REPO_ROOT, Settings, reset_settings
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

# Test modules import shared helpers (such as `task_repo`) by name; `--import-mode=importlib` adds no path.
if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))

from task_repo import copy_checkout, make_checkout


@pytest.fixture(scope="session")
def checkout_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Built once: git subprocesses cost about a second per test on Windows."""
    return make_checkout(tmp_path_factory.mktemp("checkout"))


@pytest.fixture
def checkout(tmp_path: Path, checkout_template: Path) -> Path:
    """Copy the template privately: origin/main holds M1-01 (done), M1-02 (needs M1-01) and M1-03 (needs M1-02)."""
    return copy_checkout(checkout_template, tmp_path)


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
