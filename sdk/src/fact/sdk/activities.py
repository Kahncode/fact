"""
The one place `fact.sdk` schedules an activity: from its `ActivitySpec` in `fact.core.activity_contract`.

Every step helper goes through `call_activity` (through `FactWorkflow._activity`), so no workflow or helper sets a
queue, timeout, heartbeat, retry or cancellation type by hand. A `workspace` activity goes to the queue its
`WorkspaceHandle` names; a `control` one to the control queue. The heartbeat timeout is the framework's: the worker
heartbeats every running activity.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from fact.core.activity_contract import (
        CONTROL_QUEUE,
        HEARTBEAT_TIMEOUT,
        ActivitySpec,
        Cancellation,
        Retry,
        spec_for,
    )
    from fact.core.types import WorkspaceHandle

_CANCELLATION: dict[Cancellation, workflow.ActivityCancellationType] = {
    "wait": workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
    "try": workflow.ActivityCancellationType.TRY_CANCEL,
}


def retry_policy(retry: Retry) -> RetryPolicy:
    return RetryPolicy(
        initial_interval=retry.initial_interval,
        maximum_interval=retry.maximum_interval,
        maximum_attempts=retry.maximum_attempts,
    )


def task_queue(name: str, spec: ActivitySpec, handle: WorkspaceHandle | None) -> str:
    """Return the control queue, or for a `workspace` activity the queue of the runner holding the workspace."""
    if spec.zone == "control":
        return CONTROL_QUEUE
    if handle is None:
        msg = f"{name} runs on the workspace: call `prepare` first"
        raise RuntimeError(msg)
    return handle.task_queue


async def call_activity[T](
    name: str,
    arg: object,
    result_type: type[T],
    *,
    handle: WorkspaceHandle | None,
    max_duration: timedelta | None = None,
) -> T:
    """
    Schedule activity `name` as its spec says and return its result; `max_duration` replaces the spec's default.

    The maximum duration bounds each attempt; with unlimited retries (`maximum_attempts` 0) it bounds them all.
    """
    spec = spec_for(name)
    duration = max_duration or spec.max_duration
    return await workflow.execute_activity(
        name,
        arg,
        result_type=result_type,
        task_queue=task_queue(name, spec, handle),
        start_to_close_timeout=duration,
        schedule_to_close_timeout=duration if spec.retry.maximum_attempts == 0 else None,
        heartbeat_timeout=HEARTBEAT_TIMEOUT,
        retry_policy=retry_policy(spec.retry),
        cancellation_type=_CANCELLATION[spec.cancellation],
    )
