"""Serve the real API app to the CLI in tests, and run `fact` commands against it."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from click.testing import Result
from fact.api.app import create_app
from fact.cli import projects
from fact.cli.main import app as cli
from fact.core.settings import Settings
from fastapi import FastAPI
from temporalio.client import Client
from typer.testing import CliRunner

CALL_TIMEOUT_S = 30


class TemporalStub:
    """Stands in for the API's `TemporalConnection`, handing out a test server's client."""

    address = "test"

    def __init__(self, client: Client) -> None:
        self._client = client

    async def client(self) -> Client:
        return self._client


@asynccontextmanager
async def serve_api(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FastAPI]:
    """Serve the real API app: the CLI's sync client reaches it on this test's loop while the CLI runs in a thread."""
    application = create_app(settings)
    loop = asyncio.get_running_loop()
    async with (
        application.router.lifespan_context(application),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url=settings.api_url) as client,
    ):

        def bridge(request: httpx.Request) -> httpx.Response:
            call = client.request(request.method, request.url, content=request.content, headers=request.headers)
            got = asyncio.run_coroutine_threadsafe(call, loop).result(CALL_TIMEOUT_S)
            return httpx.Response(got.status_code, content=got.content, headers={"content-type": "application/json"})

        transport = httpx.MockTransport(bridge)
        monkeypatch.setattr(
            projects, "make_client", lambda: httpx.Client(base_url=settings.api_url, transport=transport)
        )
        yield application


async def fact(*args: str) -> Result:
    """Run `fact <args>` in a thread, so the API keeps serving on the test's loop."""
    return await asyncio.to_thread(CliRunner().invoke, cli, list(args))
