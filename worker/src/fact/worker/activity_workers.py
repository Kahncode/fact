"""
The activity workers of one project version: each activity on the queue of its zone, heartbeating, versioned.

Every registered activity needs an `ActivitySpec` in `fact.core.activity_contract` (`by_zone` raises otherwise). A
`control` activity runs on the control queue; a `workspace` one on the queue the runner's handles name. Both workers
join the project's worker deployment version (`deployments.py`), so a run pinned to that version runs its activities
on workers of the same factory build as its workflow. Every activity runs inside `HeartbeatInterceptor`.
"""

from collections.abc import Callable, Sequence
from typing import Any

from fact.core.activity_contract import CONTROL_QUEUE, Zone, spec_for
from fact.worker.deployments import deployment_config
from fact.worker.heartbeats import HeartbeatInterceptor
from fact.worker.loader import ProjectWorkflows
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

type Activity = Callable[..., Any]


def activity_name(fn: Activity) -> str:
    """Return the name `fn` is registered under (its `@activity.defn` name)."""
    return activity._Definition.must_from_callable(fn).name  # noqa: SLF001 - Temporal has no public accessor


def by_zone(activities: Sequence[Activity]) -> dict[Zone, list[Activity]]:
    """Split activities by the zone their spec names; raise `UnknownActivityError` for one with no spec."""
    zones: dict[Zone, list[Activity]] = {"control": [], "workspace": []}
    for fn in activities:
        zones[spec_for(activity_name(fn)).zone].append(fn)
    return zones


def activity_workers(
    client: Client,
    project: ProjectWorkflows,
    activities: Sequence[Activity],
    *,
    workspace_queue: str,
    max_workspace_activities: int | None = None,
) -> list[Worker]:
    """Return the control and workspace workers of `project`'s version; `workspace_queue` is the runner's queue."""
    zones = by_zone(activities)
    common: dict[str, Any] = {
        "deployment_config": deployment_config(project),
        "interceptors": [HeartbeatInterceptor()],
    }
    queues = {"control": CONTROL_QUEUE, "workspace": workspace_queue}
    limits = {"control": None, "workspace": max_workspace_activities}
    return [
        Worker(client, task_queue=queues[zone], activities=fns, max_concurrent_activities=limits[zone], **common)
        for zone, fns in zones.items()
        if fns  # a worker needs at least one activity
    ]
