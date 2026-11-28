"""
Where the scheduler ticks: a loop in the API in dev, only `POST /scheduler/tick` elsewhere.

In dev the loop is on on a fresh database and switched by `fact scheduler on|off`, which an API restart keeps; elsewhere
there is no loop and no switch.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from http import HTTPStatus
from pathlib import Path

import pytest
from fact.api.app import create_app
from fact.api.scheduler_loop import scheduler_loop
from fact.core.scheduler import TickResult
from fact.core.settings import CONFIG_VAR, AppEnv, Settings, reset_settings
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from scheduler_env import FakeTemporal
from starlette.datastructures import State
from structlog.testing import capture_logs

WAIT_S = 5.0


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """No project: a tick reads nothing and starts nothing."""
    config = tmp_path / "fact.yaml"
    config.write_text(f"data_dir: {(tmp_path / 'data').as_posix()}\nprojects: []\n", encoding="utf-8")
    monkeypatch.setenv(CONFIG_VAR, str(config))
    reset_settings()
    yield Settings()
    reset_settings()


class _Clock:
    """The loop's `sleep`: each call waits until the test lets one interval pass."""

    def __init__(self) -> None:
        self.asleep: asyncio.Queue[None] = asyncio.Queue()
        self.wake: asyncio.Queue[None] = asyncio.Queue()

    async def sleep(self, _seconds: float) -> None:
        await self.asleep.put(None)
        await self.wake.get()

    async def interval(self) -> None:
        """Let one interval pass and wait until the loop sleeps again."""
        await self.wake.put(None)
        await asyncio.wait_for(self.asleep.get(), WAIT_S)


async def test_dev_loop_is_on_at_start_and_follows_the_switch(app: FastAPI, client: AsyncClient) -> None:
    loop: asyncio.Task[None] = app.state.scheduler_loop
    assert loop is not None
    assert not loop.done()
    state = (await client.get("/api/v1/scheduler")).json()
    assert (state["in_process"], state["on"], state["interval_seconds"]) == (True, True, 30.0)

    clock, ticks = _Clock(), []

    async def tick(_state: State) -> TickResult:
        ticks.append(len(ticks))
        if len(ticks) == 1:
            msg = "GitHub is down"
            raise RuntimeError(msg)
        return TickResult()

    task = asyncio.create_task(scheduler_loop(app.state, tick=tick, sleep=clock.sleep))
    await asyncio.wait_for(clock.asleep.get(), WAIT_S)
    with capture_logs() as logs:
        await clock.interval()  # the tick raises: logged, the loop goes on
    assert any(e["event"] == "scheduler_tick_failed" for e in logs)
    await clock.interval()
    assert len(ticks) == 2  # noqa: PLR2004
    assert (await client.post("/api/v1/scheduler/off")).json()["on"] is False
    await clock.interval()
    await clock.interval()
    assert len(ticks) == 2  # off: no tick  # noqa: PLR2004
    off_tick = await client.post("/api/v1/scheduler/tick")  # an explicit request still ticks
    assert off_tick.status_code == HTTPStatus.OK, off_tick.text
    assert (await client.post("/api/v1/scheduler/on")).json()["on"] is True
    await clock.interval()
    assert len(ticks) == 3  # noqa: PLR2004
    task.cancel()


async def test_off_survives_an_api_restart(settings: Settings) -> None:
    first = create_app(settings)
    async with first.router.lifespan_context(first):
        transport = ASGITransport(app=first, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.post("/api/v1/scheduler/off")).json()["on"] is False

    restarted = create_app(settings)
    async with restarted.router.lifespan_context(restarted):
        transport = ASGITransport(app=restarted, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/api/v1/scheduler")).json()["on"] is False
        clock, ticks = _Clock(), []

        async def tick(_state: State) -> TickResult:
            ticks.append(1)
            return TickResult()

        task = asyncio.create_task(scheduler_loop(restarted.state, tick=tick, sleep=clock.sleep))
        await asyncio.wait_for(clock.asleep.get(), WAIT_S)
        await clock.interval()
        assert ticks == []
        task.cancel()


@pytest.fixture
async def prod_app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings.model_copy(update={"env": AppEnv.PROD}))
    async with application.router.lifespan_context(application):
        application.state.temporal = FakeTemporal(forbid=True)
        yield application


async def test_prod_has_no_loop_and_no_switch(prod_app: FastAPI) -> None:
    assert prod_app.state.scheduler_loop is None
    transport = ASGITransport(app=prod_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post("/api/v1/scheduler/on")).status_code == HTTPStatus.NOT_FOUND
        assert (await client.post("/api/v1/scheduler/off")).status_code == HTTPStatus.NOT_FOUND
        state = (await client.get("/api/v1/scheduler")).json()
        assert (state["in_process"], state["on"]) == (False, None)
        tick = await client.post("/api/v1/scheduler/tick")
        assert tick.status_code == HTTPStatus.OK, tick.text
        assert TickResult.model_validate(tick.json()) == TickResult()
        assert (await client.get("/api/v1/scheduler")).json()["last_tick_at"] is not None
