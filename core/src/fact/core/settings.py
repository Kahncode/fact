"""
Factory settings, lowest to highest priority: defaults, fact.yaml, fact.<env>.yaml, .env, FACT_* variables.

How a project's tasks run (routing, step limits, its own cap) is project config, `fact.core.project_config`.

As everywhere in pydantic-settings, a real environment variable wins over the same key in `.env`.
"""

import os
import re
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

ENV_PREFIX = "FACT_"
CONFIG_VAR = "FACT_CONFIG"
ENV_VAR = "FACT_ENV"

# core/src/fact/core/settings.py -> repository root
REPO_ROOT = Path(__file__).resolve().parents[4]


class AppEnv(StrEnum):
    LOCAL = "local"
    TEST = "test"
    DEV = "dev"
    PROD = "prod"


LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TemporalSettings(_Section):
    address: str = "localhost:7233"
    namespace: str = "default"
    ui_url: str = "http://localhost:8233"  # Temporal Web UI, for run links
    retention_days: int = Field(default=30, ge=1)  # `fact dev` sets it on the namespace: closed histories go after it


class ApiSettings(_Section):
    host: str = "127.0.0.1"
    port: int = 8000


class ConcurrencySettings(_Section):
    scheduler_interval_seconds: float = Field(default=30.0, gt=0)  # dev only: the API's tick loop runs this often
    listing_max_age_seconds: float = Field(default=600.0, ge=0)  # a tick lists a project's tasks again past this age
    max_failed_runs: int = Field(default=3, ge=1)  # the scheduler stops restarting a task after this many failed runs


class ClaudeReserve(_Section):  # share of each Claude usage window the scheduler leaves to the person (0..1)
    five_hour: float = Field(default=0.10, ge=0, le=1)
    weekly: float = Field(default=0.05, ge=0, le=1)  # `seven_day`, `seven_day_opus` and `seven_day_sonnet`


class ClaudeSettings(_Section):
    usage_gate: bool = True  # the scheduler starts no run while a usage window is inside its reserve
    reserve: ClaudeReserve = Field(default_factory=ClaudeReserve)


class WorkerSettings(_Section):
    keep_awake: bool = True  # Windows: keep the machine awake while a run is queued, running or waiting


class ProjectRef(_Section):
    name: str
    home: str  # GitHub "owner/repo" holding the project's .fact/ folder
    path: Path | None = None  # optional local checkout


def config_file() -> Path:
    """Path of fact.yaml: `FACT_CONFIG` if set, else the one at the repository root."""
    override = os.environ.get(CONFIG_VAR)
    return Path(override).expanduser() if override else REPO_ROOT / "fact.yaml"


def project_path(ref: ProjectRef) -> Path | None:
    """Return the checkout path of a project; a relative path is relative to the folder of fact.yaml."""
    if ref.path is None:
        return None
    path = ref.path.expanduser()
    return path if path.is_absolute() else (config_file().resolve().parent / path).resolve()


def _dotenv_path() -> Path:
    return config_file().parent / ".env"


_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def expand_env_vars(obj: Any) -> Any:  # noqa: ANN401
    """
    Replace `${NAME}` in every string of a loaded YAML tree.

    The value comes from the environment variable NAME, else from the contents of the file at path NAME
    (a mounted secret). An unresolvable reference is left as written.
    """
    if isinstance(obj, dict):
        return {k: expand_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_env_vars(v) for v in obj]
    if isinstance(obj, str):

        def replace(m: re.Match[str]) -> str:
            name = m.group(1)
            if value := os.environ.get(name):
                return value
            path = Path(name)
            if path.is_file():
                return path.read_text(encoding="utf-8").strip()
            return m.group(0)

        return _VAR_PATTERN.sub(replace, obj)
    return obj


class _ExpandingYamlSource(YamlConfigSettingsSource):
    def _read_file(self, file_path: Path) -> dict[str, Any]:
        return expand_env_vars(super()._read_file(file_path))


def _resolve_env() -> str:
    """FACT_ENV from the environment, else from .env, else `local`; needed to pick the overlay file."""
    value = os.environ.get(ENV_VAR) or dotenv_values(_dotenv_path()).get(ENV_VAR)
    return value or AppEnv.LOCAL.value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter="__",
        extra="ignore",  # .env also carries secrets that later tasks add as fields
        frozen=True,
    )

    env: AppEnv = AppEnv.LOCAL
    log_level: LogLevel | None = None  # None: DEBUG in local and dev, INFO elsewhere
    log_gcp: bool = False  # JSON logs shaped for Google Cloud Logging
    data_dir: Path = Path("~/.fact")
    commit_author_name: str = "fact"
    commit_author_email: str = "fact"
    github_app_id: str | None = None  # App ID or Client ID; both are accepted as the JWT issuer
    github_app_private_key_path: Path | None = None  # PEM file, kept outside any repository
    github_api_url: str = "https://api.github.com"
    temporal: TemporalSettings = Field(default_factory=TemporalSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    concurrency: ConcurrencySettings = Field(default_factory=ConcurrencySettings)
    claude: ClaudeSettings = Field(default_factory=ClaudeSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    projects: list[ProjectRef] = Field(default_factory=list)

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, v: object) -> object:
        return v.upper() if isinstance(v, str) else v

    @field_validator("data_dir")
    @classmethod
    def _expand_home(cls, v: Path) -> Path:
        return v.expanduser()

    def is_dev_environment(self) -> bool:
        return self.env in (AppEnv.LOCAL, AppEnv.TEST, AppEnv.DEV)

    @property
    def effective_log_level(self) -> str:
        if self.log_level:
            return self.log_level
        return "DEBUG" if self.env in (AppEnv.LOCAL, AppEnv.DEV) else "INFO"

    @property
    def api_url(self) -> str:
        """Base URL clients use to reach the API; a wildcard bind host maps to loopback."""
        host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(self.api.host, self.api.host)  # noqa: S104
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"  # IPv6 literal
        return f"http://{host}:{self.api.port}"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "fact.db"

    @property
    def temporal_db_path(self) -> Path:
        return self.data_dir / "temporal.db"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def pinned_dir(self) -> Path:
        return self.data_dir / "pinned"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,  # noqa: ARG003
        dotenv_settings: PydanticBaseSettingsSource,  # noqa: ARG003
        file_secret_settings: PydanticBaseSettingsSource,  # noqa: ARG003
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        base = config_file()
        overlay = base.with_name(f"{base.stem}.{_resolve_env()}{base.suffix}")
        # Highest priority first. Missing YAML files are skipped by YamlConfigSettingsSource.
        return (
            init_settings,
            EnvSettingsSource(settings_cls),
            DotEnvSettingsSource(settings_cls, env_file=_dotenv_path()),
            _ExpandingYamlSource(settings_cls, yaml_file=overlay),
            _ExpandingYamlSource(settings_cls, yaml_file=base),
        )


_settings_class: type[Settings] = Settings
_settings_instance: Settings | None = None


def override_settings_class(new_class: type[Settings]) -> None:
    """Let an app (API, worker) load its own `Settings` subclass. Call once, before the first `get_settings()`."""
    global _settings_class  # noqa: PLW0603
    if _settings_instance is not None:
        msg = "Settings already loaded; cannot override the class"
        raise RuntimeError(msg)
    if _settings_class is not Settings:
        msg = "Settings class can only be overridden once"
        raise RuntimeError(msg)
    _settings_class = new_class


def get_settings() -> Settings:
    global _settings_instance  # noqa: PLW0603
    if _settings_instance is None:
        _settings_instance = _settings_class()
    return _settings_instance


def reset_settings() -> None:
    """Drop the cached instance and any class override so the next `get_settings()` reloads (for tests)."""
    global _settings_class, _settings_instance  # noqa: PLW0603
    _settings_class = Settings
    _settings_instance = None
