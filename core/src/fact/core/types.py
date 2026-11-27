"""
Types that cross the Temporal boundary: tasks, workspaces, step and run inputs and results.

Models are small and JSON-serializable. Large data (logs, transcripts, diffs) never lives in them: they carry an
`ArtifactRef` into the artifact store instead. Workflow authors import these from `fact.sdk.types`.
"""

from typing import Any, Literal, get_args

from fact.core.enums import RunOutcome, StepOutcome
from fact.core.step_defaults import StepDefaultsConfig
from pydantic import BaseModel, ConfigDict, Field, field_validator

ArtifactRef = str  # key in the artifact store
TaskType = Literal["feature", "chore", "bug"]
Complexity = Literal["trivial", "standard", "complex"]
Priority = Literal["urgent", "high", "medium", "low"]  # highest first
PRIORITIES: tuple[Priority, ...] = ("urgent", "high", "medium", "low")

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
    priority: Priority = "medium"
    depends_on: list[str] = Field(default_factory=list)
    path: str | None = None  # for file tasks
    require_plan_approval: bool | None = None  # the task's own setting; None: the project's
    needs_human: bool = False  # an issue carrying `fact:needs-human`: held until a person releases it


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
    task_queue: str  # the queue of the runner holding the workspace: every step on it is sent there
    repos: dict[str, str] = Field(default_factory=dict)  # full name -> worktree path
    # full name -> the branch checked out, which is the requested one or `<branch>-<n>` when another workspace holds it
    branches: dict[str, str] = Field(default_factory=dict)


class Usage(_Model):
    """
    What a harness reported a session used: never estimated, so a cost it did not report stays None.

    `tokens_in` is every prompt token (fresh input plus cache writes and cache reads); the cache counts are part of
    it. `partial` marks a session cut short before its final report: its tokens are those of the messages received.
    `turns` (model calls over the whole session) and `peak_context_tokens` (the input of its largest turn) are None
    for steps that ran no agent session.
    """

    tokens_in: int = 0
    tokens_out: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None
    partial: bool = False
    turns: int | None = None
    peak_context_tokens: int | None = None


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


DEFAULT_ROUTE = Route(harness="claude-code", model="sonnet")


class RoutingConfig(_Model):
    """
    Harness and model per `<complexity>.<stage>`; either side may be `*`. Resolution is in `fact.worker.routing`.

    A project's `routing` in `.fact/project.yaml`; `default` is used for any field no rule sets.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    default: Route = DEFAULT_ROUTE
    rules: dict[str, RouteOverride] = Field(default_factory=dict)

    @field_validator("rules")
    @classmethod
    def _rule_keys(cls, v: dict[str, RouteOverride]) -> dict[str, RouteOverride]:
        for key in v:
            complexity, dot, stage = key.partition(".")
            if not dot or not stage or "." in stage or complexity not in {*get_args(Complexity), "*"}:
                msg = f"routing rule {key!r} must be '<complexity>.<stage>' (either may be '*')"
                raise ValueError(msg)
        return v


class PRRef(_Model):
    repo: str
    number: int
    url: str
    head_sha: str


MergeMethod = Literal["squash", "merge", "rebase"]


class RunInput(_Model):
    run_id: str
    project: str
    task: Task
    repos: list[RepoCheckout] = Field(default_factory=list)
    complexity: Complexity = "standard"
    home_repo: str | None = None  # full name in `repos` holding `.fact/`; None: the first repository
    routing: RoutingConfig = Field(default_factory=RoutingConfig)  # the project's `routing`
    step_defaults: StepDefaultsConfig = Field(default_factory=StepDefaultsConfig)  # the project's overrides
    run_url: str = ""  # where a person follows the run (the Temporal UI for now), linked from the PR
    require_plan_approval: bool = False  # open an `approve_plan` gate before any work (task, else project setting)
    handoff_ref: str | None = None  # artifact key of the handoff note a stopped run of this task left, if any
    auto_merge: bool = True  # the project's `auto_merge`: fact merges a change set classified `auto_merge`
    merge_method: MergeMethod = "squash"  # the project's `merge_method`
    review_spec_ambiguous: bool = True  # the project's `classify.review_spec_ambiguous`


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
