"""
The contract between workflow code (`fact.sdk`) and the worker: task queues, activity names and activity inputs.

Workflows call activities by name, so `fact.sdk` never imports `fact.worker`; the worker registers its activities
under these names and takes these inputs. Everything here crosses the Temporal boundary: small and JSON-serializable.
"""

from datetime import datetime
from typing import Any, Literal

from fact.core.enums import NotificationKind, RunOutcome, StepOutcome
from fact.core.gates import GateDecision
from fact.core.types import Complexity, PRRef, RouteOverride, Task, WorkspaceHandle
from pydantic import BaseModel, ConfigDict, Field

CONTROL_QUEUE = "control"  # holds the GitHub App and the API; workspace, PR and bookkeeping steps
SANDBOX_QUEUE = "sandbox"  # runs agents and the repository's own commands
WORKFLOW_QUEUE_PREFIX = "wf-"  # each project's workflows run on `wf-<project>`, one worker deployment per project

PREPARE_WORKSPACE = "prepare_workspace"
TEARDOWN_WORKSPACE = "teardown_workspace"
AGENT_STEP = "agent_step"
SHELL_STEP = "shell_step"
OPEN_CHANGE_SET = "open_change_set"
MARK_TASK_DONE = "mark_task_done"
POST_COST_COMMENT = "post_cost_comment"
WAIT_FOR_CI = "wait_for_ci"
RECORD_RUN_STARTED = "record_run_started"
RECORD_RUN_FINISHED = "record_run_finished"
RECORD_GATE_OPENED = "record_gate_opened"
RECORD_GATE_ANSWERED = "record_gate_answered"
NOTIFY = "notify"

Check = Literal["lint", "test", "build", "setup"]

# `agent_step` fails with this ApplicationError type when the worker stops during the session; `details[0]` is the
# session id to resume. A worker that dies outright surfaces as a heartbeat timeout whose last heartbeat details carry
# `(progress, session_id)`.
AGENT_INTERRUPTED = "AgentInterrupted"


def workflow_queue(project: str) -> str:
    """Task queue (and worker deployment name) of a project's workflow worker."""
    return f"{WORKFLOW_QUEUE_PREFIX}{project}"


class _Input(BaseModel):
    model_config = ConfigDict(frozen=True)


class AgentStepInput(_Input):
    run_id: str
    stage_id: str
    attempt: int
    handle: WorkspaceHandle
    task: Task
    prompt_name: str  # `.fact/prompts/<prompt_name>.md` in the home repository
    context: str = ""  # `$context` in the prompt, such as failing check output
    complexity: Complexity = "standard"
    override: RouteOverride | None = None
    home_repo: str | None = None  # full name in `handle.repos` holding `.fact/`; None: the first repository
    resume_session_id: str | None = None  # resume this session with the resume prompt; `context` says why


class ShellStepInput(_Input):
    run_id: str
    stage_id: str
    attempt: int
    handle: WorkspaceHandle
    repo: str  # full name, a key of `handle.repos`
    check: Check
    timeout_s: float = 900.0  # for the whole check, all commands together


class CheckResult(_Input):
    """One check the run passed or failed, such as `lint` or `test`, for the PR description."""

    name: str
    outcome: StepOutcome
    summary: str = ""


class CriterionResult(_Input):
    """One acceptance criterion as the review session judged it, with its evidence, for the PR description."""

    text: str
    met: bool
    evidence: str = ""


class OpenChangeSetInput(_Input):
    run_id: str
    task: Task
    handle: WorkspaceHandle
    branch: str  # the branch the workspace checked out (`WorkspaceHandle.branches`); the only one this pushes
    title: str  # PR title, written by the last agent session
    summary: str  # top of the PR description, written by the last agent session
    check_results: list[CheckResult] = Field(default_factory=list)
    criteria: list[CriterionResult] = Field(default_factory=list)  # from the review; empty: the task's plain list
    run_url: str = ""
    attempt: int = 1  # of stage `open_pr`: a CI fix pushes and updates the PR again as the next attempt


class MarkTaskDoneInput(_Input):
    run_id: str
    task: Task
    handle: WorkspaceHandle
    home_repo: str | None = None  # full name in `handle.repos` holding `.fact/` and the tasks; None: the first


class PostCostCommentInput(_Input):
    run_id: str
    prs: list[PRRef]  # each gets the run's cost comment, created or updated in place


class WaitForCIInput(_Input):
    run_id: str
    stage_id: str
    attempt: int
    pr: PRRef  # CI is read for `pr.head_sha`, the commit `open_change_set` pushed
    timeout_s: float = 3600.0  # give up waiting for checks still running after this long
    no_checks_grace_s: float = 300.0  # no check or status at all after this long: the repository has no CI


class RunStartedInput(_Input):
    run_id: str
    worker_version: str | None = (
        None  # `<factory SHA>+<home SHA>`, the build id the run is pinned to; None: unversioned
    )


class RunFinishedInput(_Input):
    run_id: str
    outcome: RunOutcome


class GateOpenedInput(_Input):
    """A gate opened: its step (kind `gate`) starts waiting, and so does the run."""

    run_id: str
    stage_id: str
    attempt: int
    gate_id: str
    kind: str
    details: dict[str, Any] = Field(default_factory=dict)
    opened_at: datetime  # workflow time, so the step's wall time is the wait


class GateAnsweredInput(_Input):
    """A gate answered: its step ends with the decision and the wait, and the run runs again."""

    run_id: str
    step_id: str | None  # from `record_gate_opened`; None if that could not create the step
    stage_id: str
    attempt: int
    gate_id: str
    kind: str
    details: dict[str, Any] = Field(default_factory=dict)
    decision: GateDecision
    note: str = ""
    outcome: StepOutcome  # `passed` for approve, `needs_human` for reject
    opened_at: datetime
    answered_at: datetime
    wait_seconds: float  # human wait time: all of the step's wall time


class NotifyInput(_Input):
    """A notification for a person; a second one with the same `dedupe_key` is the same notification."""

    run_id: str
    kind: NotificationKind
    title: str = Field(max_length=300)
    body: str = ""
    link: str | None = None
    dedupe_key: str = Field(max_length=300)  # such as `<run id>:gate:<gate id>`: one per run state
