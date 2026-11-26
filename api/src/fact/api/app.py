"""The FastAPI app."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fact.api.middleware import RequestIdMiddleware, unhandled_exception_handler
from fact.api.projects import sync_projects
from fact.api.routers import artifacts, health, human, projects, run_start, runs
from fact.api.temporal import TemporalConnection
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
        try:
            await sync_projects(settings, app.state.sessions)
            yield
        finally:
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
    return app
