"""Run Alembic migrations from code (used by `fact dev` and by tests)."""

from pathlib import Path

from alembic import command
from alembic.config import Config

# core/src/fact/core/migrate.py -> core/
CORE_DIR = Path(__file__).resolve().parents[3]
ALEMBIC_INI = CORE_DIR / "alembic.ini"


def alembic_config(url: str) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.attributes["configure_logging"] = False  # keep the process logging set up by fact.core.logging
    config.set_main_option("script_location", str(CORE_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def upgrade(url: str, revision: str = "head") -> None:
    """
    Run `alembic upgrade <revision>` against `url`.

    Synchronous: the async `env.py` starts its own event loop, so don't call this from a running loop.
    """
    command.upgrade(alembic_config(url), revision)
