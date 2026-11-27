"""Every registered activity has a spec, and runs on the queue of its zone: workspace steps on the handle's queue."""

import httpx
import pytest
from fact.core.activity_contract import (
    ACTIVITY_SPECS,
    AGENT_STEP,
    CONTROL_QUEUE,
    SHELL_STEP,
    UnknownActivityError,
    spec_for,
)
from fact.core.settings import Settings
from fact.worker.__main__ import factory_activities
from fact.worker.activity_workers import activity_name, by_zone
from fact.worker.runners import LocalRunner
from standard_env import RUNNER_QUEUE
from standard_harness import FakeActivities, run_standard
from temporalio import activity


@activity.defn(name="unspecified_step")
async def unspecified_step() -> None:
    pass


async def test_every_activity_the_worker_registers_has_a_spec(settings: Settings) -> None:
    async with httpx.AsyncClient() as http:
        activities = factory_activities(settings, http, LocalRunner(settings), None)
    zones = by_zone(activities)
    assert sorted(activity_name(fn) for fns in zones.values() for fn in fns) == sorted(ACTIVITY_SPECS)


def test_an_activity_without_a_spec_is_refused() -> None:
    with pytest.raises(UnknownActivityError, match="unspecified_step"):
        by_zone([unspecified_step])


async def test_workspace_steps_run_on_the_queue_the_handle_names() -> None:
    fakes = FakeActivities()
    await run_standard(fakes, "run-routed")
    ran = {name for name, _ in fakes.queues}
    assert {AGENT_STEP, SHELL_STEP} <= ran
    expected = {
        name: RUNNER_QUEUE if spec_for(name).zone == "workspace" else CONTROL_QUEUE for name in ran
    }  # the fake handle names `RUNNER_QUEUE`, not the runner's default `sandbox`
    assert {(name, queue) for name, queue in fakes.queues} == set(expected.items())
