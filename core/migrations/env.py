"""Alembic environment: async engine, URL from fact settings, `Base.metadata` as target."""

import asyncio
from logging.config import fileConfig

from alembic import context
from fact.core import models  # noqa: F401  (registers tables on Base.metadata)
from fact.core.db import Base, database_url, ensure_db_dir
from fact.core.settings import get_settings
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", database_url(get_settings()).replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    ensure_db_dir(config.get_main_option("sqlalchemy.url"))
    # No fact pragmas here on purpose: batch migrations on SQLite need foreign_keys off.
    engine = async_engine_from_config(config.get_section(config.config_ini_section, {}), poolclass=NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
