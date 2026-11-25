"""
The contract between workflow code (`fact.sdk`) and the worker: task queues, activity names and activity inputs.

Workflows call activities by name, so `fact.sdk` never imports `fact.worker`; the worker registers its activities
under these names and takes these inputs. Everything here crosses the Temporal boundary: small and JSON-serializable.
"""

from typing import Literal

from fact.core.enums import RunOutcome, StepOutcome
from fact.core.types import Complexity, RouteOverride, Task, WorkspaceHandle
from pydantic import BaseModel, ConfigDict, Field

CONTROL_QUEUE = "control"  # holds the GitHub App and the API; workspace, PR and bookkeeping steps
SANDBOX_QUEUE = "sandbox"  # runs agents and the repository's own commands
WORKFLOW_QUEUE_PREFIX = "wf-"  # each project's workflows run on `wf-<project>`, one worker deployment per project

PREPARE_WORKSPACE = "prepare_workspace"
AGENT_STEP = "agent_step"
SHELL_STEP = "shell_step"
OPEN_CHANGE_SET = "open_change_set"
MARK_TASK_DONE = "mark_task_done"
RECORD_RUN_STARTED = "record_run_started"
RECORD_RUN_FINISHED = "record_run_finished"

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


class OpenChangeSetInput(_Input):
    run_id: str
    task: Task
    handle: WorkspaceHandle
    branch: str  # the run's branch, from its `RepoCheckout`; the only branch this activity pushes
    title: str  # PR title, written by the last agent session
    summary: str  # top of the PR description, written by the last agent session
    check_results: list[CheckResult] = Field(default_factory=list)
    run_url: str = ""


class MarkTaskDoneInput(_Input):
    run_id: str
    task: Task
    handle: WorkspaceHandle
    home_repo: str | None = None  # full name in `handle.repos` holding `.fact/` and the tasks; None: the first


class RunStartedInput(_Input):
    run_id: str
    worker_version: str | None = (
        None  # `<factory SHA>+<home SHA>`, the build id the run is pinned to; None: unversioned
    )


class RunFinishedInput(_Input):
    run_id: str
    outcome: RunOutcome
