"""Async SQLAlchemy 2 setup on SQLite (aiosqlite). Plain ORM: no repositories, no service wrapper."""

from pathlib import Path
from typing import Any

from fact.core.settings import Settings
from sqlalchemy import MetaData, event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

# Explicit constraint names keep later (Postgres) migrations predictable.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def database_url(settings: Settings) -> str:
    return f"sqlite+aiosqlite:///{settings.db_path.as_posix()}"


def _set_sqlite_pragmas(dbapi_connection: Any, _record: object) -> None:  # noqa: ANN401
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()


def ensure_db_dir(url: str) -> None:
    """Create the folder of a SQLite file URL; SQLite won't, and a fresh checkout has no data dir."""
    database = make_url(url).database
    if database and database != ":memory:":
        Path(database).parent.mkdir(parents=True, exist_ok=True)


def create_engine(url: str) -> AsyncEngine:
    ensure_db_dir(url)
    engine = create_async_engine(url)
    event.listen(engine.sync_engine, "connect", _set_sqlite_pragmas)
    return engine


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
