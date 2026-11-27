"""
The activity workers of one project version: each activity on the queue of its zone, heartbeating, versioned.

Every registered activity needs an `ActivitySpec` in `fact.core.activity_contract` (`by_zone` raises otherwise). A
`control` activity runs on the control queue; a `workspace` one on the queue the runner's handles name. Both workers
join the project's worker deployment version (`deployments.py`), so a run pinned to that version runs its activities
on workers of the same factory build as its workflow. Every activity runs inside `HeartbeatInterceptor`.

The runner pool's cap holds across versions: every version's workspace worker takes its slots from one `PoolSlots`,
so the process runs at most the pool's cap of workspace activities whatever the number of loaded projects.
"""

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

from fact.core.activity_contract import CONTROL_QUEUE, Zone, spec_for
from fact.worker.deployments import deployment_config
from fact.worker.heartbeats import HeartbeatInterceptor
from fact.worker.loader import ProjectWorkflows
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import (
    CustomSlotSupplier,
    FixedSizeSlotSupplier,
    SlotMarkUsedContext,
    SlotPermit,
    SlotReleaseContext,
    SlotReserveContext,
    Worker,
    WorkerTuner,
)

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


class PoolSlots(CustomSlotSupplier):
    """
    A runner pool's activity slots, shared by the workspace workers of every project version.

    A worker reserves a slot before it polls, so a full pool stops every version's worker from taking work; waiters
    get slots in the order they asked. An idle worker holds its slot until its poll comes back empty (one long poll,
    about a minute), which is why each workspace worker keeps one poll open at a time.
    """

    def __init__(self, cap: int) -> None:
        self._free = asyncio.Semaphore(cap)

    async def reserve_slot(self, ctx: SlotReserveContext) -> SlotPermit:  # noqa: ARG002 - Temporal's signature
        await self._free.acquire()
        return SlotPermit()

    def try_reserve_slot(self, ctx: SlotReserveContext) -> SlotPermit | None:  # noqa: ARG002 - Temporal's signature
        return None  # eager activities start only on the workflow's own queue, never on a runner's

    def mark_slot_used(self, ctx: SlotMarkUsedContext) -> None:
        pass

    def release_slot(self, ctx: SlotReleaseContext) -> None:  # noqa: ARG002 - Temporal's signature
        self._free.release()


def _pool_tuner(slots: PoolSlots) -> WorkerTuner:
    unused = FixedSizeSlotSupplier(1)  # a workspace worker runs no workflows, local activities or Nexus tasks
    return WorkerTuner.create_composite(
        workflow_supplier=unused, activity_supplier=slots, local_activity_supplier=unused, nexus_supplier=unused
    )


def activity_workers(
    client: Client,
    project: ProjectWorkflows,
    activities: Sequence[Activity],
    *,
    workspace_queue: str,
    workspace_slots: PoolSlots | None = None,
) -> list[Worker]:
    """
    Return the control and workspace workers of `project`'s version; `workspace_queue` is the runner's queue.

    `workspace_slots` is the runner pool's cap: pass the same `PoolSlots` for every project version.
    """
    zones = by_zone(activities)
    common: dict[str, Any] = {
        "deployment_config": deployment_config(project),
        "interceptors": [HeartbeatInterceptor()],
    }
    pool: dict[str, Any] = {}
    if workspace_slots is not None:
        pool = {"tuner": _pool_tuner(workspace_slots), "max_concurrent_activity_task_polls": 1}
    options: dict[Zone, dict[str, Any]] = {
        "control": {"task_queue": CONTROL_QUEUE},
        "workspace": {"task_queue": workspace_queue, **pool},
    }
    return [
        Worker(client, activities=fns, **options[zone], **common)
        for zone, fns in zones.items()
        if fns  # a worker needs at least one activity
    ]
