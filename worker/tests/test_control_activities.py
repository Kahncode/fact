"""Control activities: recording the run's start, waits and end through the runs API."""

from fact.core.activity_contract import RunFinishedInput, RunStartedInput, RunWaitingInput
from fact.core.enums import RunOutcome
from fact.worker.activities.control import ControlActivities
from httpx import AsyncClient

BASE = "http://test"


async def test_record_run_started_and_finished(client: AsyncClient, run_id: str) -> None:
    activities = ControlActivities(BASE, client)

    await activities.record_run_started(RunStartedInput(run_id=run_id))
    run = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert run["status"] == "running"
    assert run["started_at"] is not None

    await activities.record_run_finished(RunFinishedInput(run_id=run_id, outcome=RunOutcome.NEEDS_HUMAN))
    run = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert (run["status"], run["outcome"]) == ("finished", "needs_human")
    assert run["ended_at"] is not None


async def test_record_run_waiting_and_running_again(client: AsyncClient, run_id: str) -> None:
    activities = ControlActivities(BASE, client)
    await activities.record_run_started(RunStartedInput(run_id=run_id))

    await activities.record_run_waiting(RunWaitingInput(run_id=run_id, waiting=True, reason="usage limit until 15:00"))
    assert (await client.get(f"/api/v1/runs/{run_id}")).json()["status"] == "waiting"
    await activities.record_run_waiting(RunWaitingInput(run_id=run_id, waiting=False))
    assert (await client.get(f"/api/v1/runs/{run_id}")).json()["status"] == "running"


async def test_record_run_started_records_the_pinned_version(client: AsyncClient, run_id: str) -> None:
    home = "0123456789abcdef0123456789abcdef01234567"  # pragma: allowlist secret
    version = f"fedcba987654+{home}"

    await ControlActivities(BASE, client).record_run_started(RunStartedInput(run_id=run_id, worker_version=version))

    run = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert (run["worker_version"], run["home_sha"]) == (version, home)
