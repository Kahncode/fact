"""
The contract between workflow code (`fact.sdk`) and the worker: task queues, activity names, specs and inputs.

Workflows call activities by name, so `fact.sdk` never imports `fact.worker`; the worker registers its activities
under these names and takes these inputs (`fact.core.activity_inputs`, re-exported here). Each name has an
`ActivitySpec` in `SPECS`: its zone, default maximum duration, retry and cancellation, which the SDK's one call helper
applies, so no workflow or step sets them by hand. Every activity heartbeats (the worker's framework does it), so the
heartbeat timeout is the framework's, not the spec's.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from fact.core.activity_inputs import (
    AgentStepInput,
    Check,
    CheckResult,
    ClassifySignalsInput,
    CriterionResult,
    GateAnsweredInput,
    GateOpenedInput,
    HandoffInput,
    HandoffStep,
    LabelChangeSetInput,
    MarkTaskDoneInput,
    MergeChangeSetInput,
    MergeResult,
    NotifyInput,
    OpenChangeSetInput,
    PostCostCommentInput,
    ReportStatusInput,
    RunFinishedInput,
    RunStartedInput,
    RunWaitingInput,
    ShellStepInput,
    SyncBaseInput,
    TaskEvent,
    WaitForCIInput,
)
from fact.core.review_inputs import (
    CollectCommentsInput,
    Comment,
    CommentKind,
    PostRepliesInput,
    Reply,
    RequestReviewInput,
    ReviewEvent,
    ReviewEventKind,
    WaitForReviewInput,
)
from fact.core.types import MergeMethod

__all__ = [
    "ACTIVITY_SPECS",
    "AGENT_INTERRUPTED",
    "AGENT_STEP",
    "CLASSIFY_SIGNALS",
    "COLLECT_COMMENTS",
    "CONTROL_DURATION",
    "CONTROL_QUEUE",
    "HEARTBEAT_INTERVAL",
    "HEARTBEAT_TIMEOUT",
    "INFRA_RETRY",
    "LABEL_CHANGE_SET",
    "LONG_WAIT_RETRY",
    "MARK_TASK_DONE",
    "MERGE_CHANGE_SET",
    "NOTIFY",
    "NO_RETRY",
    "OPEN_CHANGE_SET",
    "POST_COST_COMMENT",
    "POST_REPLIES",
    "PREPARE_WORKSPACE",
    "RECORD_GATE_ANSWERED",
    "RECORD_GATE_OPENED",
    "RECORD_RUN_FINISHED",
    "RECORD_RUN_STARTED",
    "RECORD_RUN_WAITING",
    "REPORT_STATUS",
    "REQUEST_REVIEW",
    "REVIEW_TIMEOUT",
    "SANDBOX_QUEUE",
    "SHELL_STEP",
    "SYNC_BASE",
    "TEARDOWN_WORKSPACE",
    "WAIT_FOR_CI",
    "WAIT_FOR_REVIEW",
    "WORKFLOW_QUEUE_PREFIX",
    "WRITE_HANDOFF",
    "ActivitySpec",
    "AgentStepInput",
    "Cancellation",
    "Check",
    "CheckResult",
    "ClassifySignalsInput",
    "CollectCommentsInput",
    "Comment",
    "CommentKind",
    "CriterionResult",
    "GateAnsweredInput",
    "GateOpenedInput",
    "HandoffInput",
    "HandoffStep",
    "LabelChangeSetInput",
    "MarkTaskDoneInput",
    "MergeChangeSetInput",
    "MergeMethod",
    "MergeResult",
    "NotifyInput",
    "OpenChangeSetInput",
    "PostCostCommentInput",
    "PostRepliesInput",
    "Reply",
    "ReportStatusInput",
    "RequestReviewInput",
    "Retry",
    "ReviewEvent",
    "ReviewEventKind",
    "RunFinishedInput",
    "RunStartedInput",
    "RunWaitingInput",
    "ShellStepInput",
    "SyncBaseInput",
    "TaskEvent",
    "UnknownActivityError",
    "WaitForCIInput",
    "WaitForReviewInput",
    "Zone",
    "spec_for",
    "workflow_queue",
]

CONTROL_QUEUE = "control"  # holds the GitHub App and the API; PR, tracker and bookkeeping steps
SANDBOX_QUEUE = "sandbox"  # the local runner's queue: agents, the repository's own commands, git in the worktree
WORKFLOW_QUEUE_PREFIX = "wf-"  # each project's workflows run on `wf-<project>`, one worker deployment per project

PREPARE_WORKSPACE = "prepare_workspace"
TEARDOWN_WORKSPACE = "teardown_workspace"
AGENT_STEP = "agent_step"
SHELL_STEP = "shell_step"
OPEN_CHANGE_SET = "open_change_set"
MARK_TASK_DONE = "mark_task_done"
POST_COST_COMMENT = "post_cost_comment"
WAIT_FOR_CI = "wait_for_ci"
WAIT_FOR_REVIEW = "wait_for_review"
MERGE_CHANGE_SET = "merge_change_set"
SYNC_BASE = "sync_base"
RECORD_RUN_STARTED = "record_run_started"
RECORD_RUN_FINISHED = "record_run_finished"
RECORD_RUN_WAITING = "record_run_waiting"
RECORD_GATE_OPENED = "record_gate_opened"
RECORD_GATE_ANSWERED = "record_gate_answered"
NOTIFY = "notify"
WRITE_HANDOFF = "write_handoff"
REPORT_STATUS = "report_status"
CLASSIFY_SIGNALS = "classify_signals"
LABEL_CHANGE_SET = "label_change_set"
COLLECT_COMMENTS = "collect_comments"
POST_REPLIES = "post_replies"
REQUEST_REVIEW = "request_review"

# `agent_step` fails with this ApplicationError type when the worker stops during the session; `details[0]` is the
# session id to resume. A worker that dies outright surfaces as a heartbeat timeout whose last heartbeat details carry
# `(progress, session_id)`, the details the step hands the worker's heartbeat wrapper.
AGENT_INTERRUPTED = "AgentInterrupted"

HEARTBEAT_INTERVAL = timedelta(seconds=30)  # the worker heartbeats every running activity this often
HEARTBEAT_TIMEOUT = timedelta(minutes=2)  # an attempt not heard from for this long is given up and retried

# `control`: the control queue; `workspace`: the queue the run's `WorkspaceHandle` names (its runner's)
Zone = Literal["control", "workspace"]
# `wait`: a cancelled activity is awaited until it has stopped, so nothing it does overlaps what comes next;
# `try`: the workflow goes on at once
Cancellation = Literal["wait", "try"]


@dataclass(frozen=True)
class Retry:
    """A retry policy; `maximum_attempts` 0 retries until the activity's maximum duration has passed."""

    maximum_attempts: int
    initial_interval: timedelta = timedelta(seconds=5)
    maximum_interval: timedelta = timedelta(minutes=2)


NO_RETRY = Retry(maximum_attempts=1)  # agent sessions are resumed only by `FactWorkflow.agent`, deliberately
INFRA_RETRY = Retry(maximum_attempts=5)  # only infrastructure errors raise, and those may pass on retry
LONG_WAIT_RETRY = Retry(maximum_attempts=0)  # a wait of days outlives worker restarts


@dataclass(frozen=True)
class ActivitySpec:
    """How an activity runs: where, for how long at most (a step may ask for another), its retries and cancellation."""

    zone: Zone
    max_duration: timedelta
    retry: Retry = INFRA_RETRY
    cancellation: Cancellation = "try"


CONTROL_DURATION = timedelta(minutes=10)
REVIEW_TIMEOUT = timedelta(days=30)  # all of a run's waits for review together; then the run fails
_CONTROL = ActivitySpec("control", CONTROL_DURATION)

ACTIVITY_SPECS: dict[str, ActivitySpec] = {
    # control: no handle exists yet; a stale attempt must stop before its retry, which returns its workspace
    PREPARE_WORKSPACE: ActivitySpec("control", CONTROL_DURATION, cancellation="wait"),
    TEARDOWN_WORKSPACE: ActivitySpec("workspace", CONTROL_DURATION),
    # a stop tears the workspace down only once the session, the check or git has stopped in it
    AGENT_STEP: ActivitySpec("workspace", timedelta(minutes=60), NO_RETRY, "wait"),
    SHELL_STEP: ActivitySpec("workspace", timedelta(minutes=16), cancellation="wait"),
    SYNC_BASE: ActivitySpec("workspace", timedelta(minutes=30), cancellation="wait"),  # a fetch and a merge per repo
    OPEN_CHANGE_SET: _CONTROL,
    LABEL_CHANGE_SET: _CONTROL,
    MARK_TASK_DONE: _CONTROL,
    POST_COST_COMMENT: _CONTROL,
    WAIT_FOR_CI: ActivitySpec("control", timedelta(minutes=65)),  # CI's 60 min, plus polling and log downloads
    WAIT_FOR_REVIEW: ActivitySpec("control", REVIEW_TIMEOUT, LONG_WAIT_RETRY),  # the run passes what is left of it
    MERGE_CHANGE_SET: _CONTROL,
    RECORD_RUN_STARTED: _CONTROL,
    RECORD_RUN_FINISHED: _CONTROL,
    RECORD_RUN_WAITING: _CONTROL,
    RECORD_GATE_OPENED: _CONTROL,
    RECORD_GATE_ANSWERED: _CONTROL,
    NOTIFY: _CONTROL,
    WRITE_HANDOFF: _CONTROL,
    REPORT_STATUS: _CONTROL,
    CLASSIFY_SIGNALS: _CONTROL,
    COLLECT_COMMENTS: _CONTROL,
    POST_REPLIES: _CONTROL,
    REQUEST_REVIEW: _CONTROL,
}


class UnknownActivityError(KeyError):
    """An activity name with no `ActivitySpec`: declare it in `ACTIVITY_SPECS` before calling or registering it."""


def spec_for(name: str) -> ActivitySpec:
    """Return the spec of activity `name`; raise `UnknownActivityError` when it has none."""
    try:
        return ACTIVITY_SPECS[name]
    except KeyError:
        msg = f"activity {name!r} has no spec in fact.core.activity_contract.ACTIVITY_SPECS"
        raise UnknownActivityError(msg) from None


def workflow_queue(project: str) -> str:
    """Task queue (and worker deployment name) of a project's workflow worker."""
    return f"{WORKFLOW_QUEUE_PREFIX}{project}"
