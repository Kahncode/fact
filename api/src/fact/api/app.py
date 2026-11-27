"""The FastAPI app."""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fact.api.middleware import RequestIdMiddleware, unhandled_exception_handler
from fact.api.projects import sync_projects
from fact.api.routers import artifacts, health, human, notifications, projects, run_start, runs, scheduler, usage
from fact.api.scheduler import SchedulerRuntime
from fact.api.scheduler_loop import scheduler_loop
from fact.api.temporal import TemporalConnection
from fact.api.usage_gate import UsageGate
from fact.core import migrate
from fact.core.artifacts import LocalArtifactStore
from fact.core.db import create_engine, database_url, session_factory
from fact.core.logging import configure_logging
from fact.core.settings import Settings, get_settings
from fastapi import FastAPI


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.env, settings.effective_log_level, gcp=settings.log_gcp)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        url = database_url(settings)
        # upgrade() starts its own event loop, so it runs in a thread.
        await asyncio.to_thread(migrate.upgrade, url)
        engine = create_engine(url)
        app.state.settings = settings
        app.state.engine = engine
        app.state.sessions = session_factory(engine)
        app.state.artifacts = LocalArtifactStore(settings.artifacts_dir)
        app.state.temporal = TemporalConnection(settings)
        app.state.scheduler = SchedulerRuntime(gate=UsageGate(settings.claude))
        loop: asyncio.Task[None] | None = None
        try:
            await sync_projects(settings, app.state.sessions)
            if settings.is_dev_environment():  # elsewhere only POST /scheduler/tick ticks
                loop = asyncio.create_task(scheduler_loop(app.state), name="scheduler-loop")
            app.state.scheduler_loop = loop
            yield
        finally:
            if loop is not None:
                loop.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await loop
            await engine.dispose()

    app = FastAPI(title="fact", lifespan=lifespan)
    app.add_middleware(RequestIdMiddleware)
    app.add_exception_handler(Exception, unhandled_exception_handler)
    app.include_router(health.router)
    app.include_router(projects.router)
    app.include_router(run_start.router)
    app.include_router(runs.router)
    app.include_router(human.router)
    app.include_router(artifacts.router)
    app.include_router(notifications.router)
    app.include_router(scheduler.router)
    if settings.is_dev_environment():
        app.include_router(scheduler.dev_router)  # the on/off switch exists only in dev
    app.include_router(usage.router)
    return app
