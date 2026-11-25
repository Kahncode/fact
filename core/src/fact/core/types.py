"""
Types that cross the Temporal boundary: tasks, workspaces, step and run inputs and results.

Models are small and JSON-serializable. Large data (logs, transcripts, diffs) never lives in them: they carry an
`ArtifactRef` into the artifact store instead. Workflow authors import these from `fact.sdk.types`.
"""

from typing import Any, Literal

from fact.core.enums import RunOutcome, StepOutcome
from pydantic import BaseModel, ConfigDict, Field, field_validator

ArtifactRef = str  # key in the artifact store
TaskType = Literal["feature", "chore", "bug"]
Complexity = Literal["trivial", "standard", "complex"]

SUMMARY_MAX_BYTES = 2048


def limit_text(s: str, max_bytes: int) -> str:
    """Cut `s` to at most `max_bytes` of UTF-8, on a character boundary. Use for any text that goes in a payload."""
    if max_bytes <= 0:
        return ""
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return s
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class Task(_Model):
    key: str  # such as "task:M1-01"
    id: str
    title: str
    body: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    type: TaskType = "feature"
    complexity: Complexity = "standard"
    depends_on: list[str] = Field(default_factory=list)
    path: str | None = None  # for file tasks


class RepoCheckout(_Model):
    full_name: str  # GitHub "owner/repo"
    default_branch: str
    branch: str
    role: str = ""  # shown in WORKSPACE.md, such as "backend"


class WorkspaceSpec(_Model):
    run_id: str
    task_key: str
    repos: list[RepoCheckout]


class WorkspaceHandle(_Model):
    run_id: str
    root: str
    repos: dict[str, str] = Field(default_factory=dict)  # full name -> worktree path


class Usage(_Model):
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0


class StepResult(_Model):
    outcome: StepOutcome
    summary: str = Field(default="")
    outputs: dict[str, Any] = Field(default_factory=dict)
    log_ref: ArtifactRef | None = None
    transcript_ref: ArtifactRef | None = None
    usage: Usage = Field(default_factory=Usage)
    session_id: str | None = None  # agent steps: the harness session, to resume it

    @field_validator("summary")
    @classmethod
    def _summary_fits(cls, v: str) -> str:
        if len(v.encode("utf-8")) > SUMMARY_MAX_BYTES:
            msg = f"summary is over {SUMMARY_MAX_BYTES} bytes; cut it with limit_text"
            raise ValueError(msg)
        return v


class RouteOverride(_Model):
    """A partial harness and model choice: a routing rule, or a step-level override. Unset fields fall through."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    harness: str | None = None
    model: str | None = None


class Route(_Model):
    """The harness and model an agent step runs on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    harness: str
    model: str


class PRRef(_Model):
    repo: str
    number: int
    url: str
    head_sha: str


class RunLimits(_Model):
    """Activity timeouts for a run, set by whoever starts it (from `step_defaults`): workflow code reads no settings."""

    agent_timeout_s: float = Field(default=3600.0, gt=0)
    shell_timeout_s: float = Field(default=900.0, gt=0)
    control_timeout_s: float = Field(default=600.0, gt=0)


class RunInput(_Model):
    run_id: str
    project: str
    task: Task
    repos: list[RepoCheckout] = Field(default_factory=list)
    complexity: Complexity = "standard"
    home_repo: str | None = None  # full name in `repos` holding `.fact/`; None: the first repository
    limits: RunLimits = Field(default_factory=RunLimits)
    run_url: str = ""  # where a person follows the run (the Temporal UI for now), linked from the PR


class RunResult(_Model):
    outcome: RunOutcome
    prs: list[PRRef] = Field(default_factory=list)
    summary: str = Field(default="")

    @field_validator("summary")
    @classmethod
    def _summary_fits(cls, v: str) -> str:
        if len(v.encode("utf-8")) > SUMMARY_MAX_BYTES:
            msg = f"summary is over {SUMMARY_MAX_BYTES} bytes; cut it with limit_text"
            raise ValueError(msg)
        return v
