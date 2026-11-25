from pathlib import Path

from fact.core.db import Base, create_engine, database_url, session_factory
from fact.core.migrate import upgrade
from fact.core.settings import Settings
from sqlalchemy import text


def _url(tmp_path: Path) -> str:
    return database_url(Settings(data_dir=tmp_path))


def test_database_url_uses_db_path(tmp_path: Path) -> None:
    assert _url(tmp_path) == f"sqlite+aiosqlite:///{(tmp_path / 'fact.db').as_posix()}"


def test_upgrade_with_zero_migrations_creates_missing_data_dir(tmp_path: Path) -> None:
    data_dir = tmp_path / "not" / "yet"
    url = _url(data_dir)
    upgrade(url)
    upgrade(url)  # idempotent
    assert (data_dir / "fact.db").exists()


async def test_session_runs_select_and_pragmas(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    try:
        async with session_factory(engine)() as session:
            assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
            assert (await session.execute(text("PRAGMA foreign_keys"))).scalar_one() == 1
            assert (await session.execute(text("PRAGMA journal_mode"))).scalar_one() == "wal"
    finally:
        await engine.dispose()


def test_naming_convention_names_constraints() -> None:
    assert Base.metadata.naming_convention["pk"] == "pk_%(table_name)s"
