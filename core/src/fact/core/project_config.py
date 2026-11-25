"""Project config: `.fact/project.yaml` in a project's home repository and `.fact/repo.yaml` in each repository."""

from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

FACT_DIR = ".fact"
PROJECT_FILE = "project.yaml"
REPO_FILE = "repo.yaml"

Command = list[str]  # an argv list, never a shell string


class ConfigError(ValueError):
    """A config file is missing, unreadable or invalid. The message names the file and the field."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RepositoryConfig(_Model):
    full_name: str  # GitHub "owner/repo"
    role: str
    default_branch: str = "main"
    merge_order: int = 0


class TaskSourceConfig(_Model):
    kind: Literal["files", "github"] = "files"
    path: str = "tasks"  # for `files`: folder holding the task files
    repo: str | None = None  # for `github`: the repository whose issues are the tasks

    @model_validator(mode="after")
    def _github_needs_repo(self) -> Self:
        if self.kind == "github" and not self.repo:
            msg = "repo is required when kind is github"
            raise ValueError(msg)
        return self


class WorkflowsConfig(_Model):
    """Maps a task type label to a workflow name; `default` is used when no label matches."""

    model_config = ConfigDict(extra="allow", frozen=True)

    default: str

    @property
    def by_type(self) -> dict[str, str]:
        return {k: v for k, v in (self.model_extra or {}).items() if isinstance(v, str)}

    @model_validator(mode="after")
    def _extras_are_names(self) -> Self:
        for key, value in (self.model_extra or {}).items():
            if not isinstance(value, str):
                msg = f"{key}: workflow name must be a string"
                raise ValueError(msg)  # noqa: TRY004
        return self


class ProjectConfig(_Model):
    name: str
    repositories: list[RepositoryConfig] = Field(min_length=1)
    task_source: TaskSourceConfig = TaskSourceConfig()
    runner_pool: str = "local"
    budget_usd_per_day: float | None = Field(default=None, ge=0)
    merge_policy: Literal["human"] = "human"
    require_plan_approval: bool = True
    workflows: WorkflowsConfig


class RepoConfig(_Model):
    setup: list[Command] = []
    lint: list[Command] = []
    test: list[Command] = []
    build: list[Command] = []


def _format_errors(path: Path, err: ValidationError) -> str:
    lines = [f"{path}: invalid config"]
    for e in err.errors():
        field = ".".join(str(p) for p in e["loc"]) or "<root>"
        lines.append(f"  {field}: {e['msg']}")
    return "\n".join(lines)


def _load[T: BaseModel](path: Path, model: type[T]) -> T:
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        msg = f"{path}: file not found"
        raise ConfigError(msg) from None
    except (OSError, yaml.YAMLError) as exc:
        msg = f"{path}: cannot read file: {exc}"
        raise ConfigError(msg) from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        msg = f"{path}: expected a mapping at the top level"
        raise ConfigError(msg)
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_format_errors(path, exc)) from exc


def load_project_config(home_path: Path) -> ProjectConfig:
    """Load `<home_path>/.fact/project.yaml`."""
    return _load(Path(home_path) / FACT_DIR / PROJECT_FILE, ProjectConfig)


def load_repo_config(repo_path: Path) -> RepoConfig:
    """Load `<repo_path>/.fact/repo.yaml`."""
    return _load(Path(repo_path) / FACT_DIR / REPO_FILE, RepoConfig)
