"""`Standard` merges the base only when GitHub says so: a PR that conflicts with it, or a merge refused as out of date."""

from fact.core.activity_contract import (
    MERGE_CHANGE_SET,
    NOTIFY,
    OPEN_CHANGE_SET,
    SYNC_BASE,
    WAIT_FOR_CI,
    NotifyInput,
    SyncBaseInput,
)
from fact.core.enums import NotificationKind, RunOutcome, StepOutcome
from fact.core.types import RunResult
from standard_harness import (
    MAX_BASE_SYNCS,
    MAX_FIX_ATTEMPTS,
    FakeActivities,
    agent_sessions,
    checks,
    finished,
    run_standard,
)
from standard_inputs import REPO

RESOLVE = ("resolve_conflicts", 1, "resolve_conflicts")
CHECKS = 2  # lint and test, one round
TWICE = 2


def _syncs(fakes: FakeActivities) -> list[tuple[int, bool]]:
    specs: list[SyncBaseInput] = fakes.of(SYNC_BASE)
    return [(spec.attempt, spec.verify_only) for spec in specs]


def _resolves(fakes: FakeActivities) -> list[tuple[str, int, str]]:
    return [s for s in agent_sessions(fakes) if s[0] == "resolve_conflicts"]


def _needs_a_person(fakes: FakeActivities, result: RunResult) -> bool:
    """Tell whether the run ended `needs_human` with no gate: the run-end hold asks the person on the task."""
    return result.outcome == RunOutcome.NEEDS_HUMAN and fakes.gates("record_gate_opened") == []


async def test_a_pr_github_can_merge_never_merges_the_base() -> None:
    fakes = FakeActivities(merges=True)
    result, _ = await run_standard(fakes, "run-sync-none")

    assert result.outcome == RunOutcome.MERGED
    assert fakes.of(SYNC_BASE) == []
    assert len(fakes.of(OPEN_CHANGE_SET)) == 1


async def test_a_conflicting_pr_merges_the_base_pushes_and_waits_for_ci_again() -> None:
    fakes = FakeActivities(merges=True, ci_conflicts=1, sync_script=["merged"])
    result, _ = await run_standard(fakes, "run-sync-ci-conflict")

    assert result.outcome == RunOutcome.MERGED
    assert _syncs(fakes) == [(1, False)]
    (sync,) = fakes.of(SYNC_BASE)
    assert [r.full_name for r in sync.repos] == [REPO]
    assert _resolves(fakes) == []
    assert len(checks(fakes)) == CHECKS  # a clean merge is left to CI
    assert len(fakes.of(OPEN_CHANGE_SET)) == TWICE
    assert len(fakes.of(WAIT_FOR_CI)) == TWICE
    assert [s for s in agent_sessions(fakes) if s[0] == "fix_ci"] == []


async def test_conflicts_go_to_an_agent_and_the_checks_run_before_the_push() -> None:
    fakes = FakeActivities(merges=True, ci_conflicts=1, sync_script=["conflicted", "up_to_date"])
    result, _ = await run_standard(fakes, "run-sync-ci-resolve")

    assert result.outcome == RunOutcome.MERGED
    assert _syncs(fakes) == [(1, False), (2, True)]  # the second only checks the agent concluded the merge
    assert _resolves(fakes) == [RESOLVE]
    resolve = next(s for s in fakes.of("agent_step") if s.prompt_name == "resolve_conflicts")
    assert "docs/ARCHITECTURE.md" in resolve.context
    assert len(checks(fakes)) == 2 * CHECKS  # once before the PR, once after the resolution
    names = [n for n, _ in fakes.calls]
    last_check = max(i for i, (_, s) in enumerate(fakes.calls) if s in checks(fakes))
    second_push = [i for i, n in enumerate(names) if n == OPEN_CHANGE_SET][1]
    assert last_check < second_push


async def test_unresolved_conflicts_end_the_run_needing_a_person() -> None:
    fakes = FakeActivities(ci_conflicts=1, sync_script=["conflicted"] * 3, resolve_outcome=StepOutcome.NEEDS_HUMAN)
    result, _ = await run_standard(fakes, "run-sync-stuck")

    assert _needs_a_person(fakes, result)
    assert finished(fakes) == RunOutcome.NEEDS_HUMAN
    assert _resolves(fakes) == [RESOLVE]  # a session that does not pass is not tried again
    assert "unresolved" in result.summary
    assert len(fakes.of(OPEN_CHANGE_SET)) == 1


async def test_a_resolution_that_breaks_the_checks_ends_the_run_unpushed() -> None:
    fakes = FakeActivities(
        ci_conflicts=1, sync_script=["conflicted", "up_to_date"], test_failures_after_resolve=MAX_FIX_ATTEMPTS + 1
    )
    result, _ = await run_standard(fakes, "run-sync-ci-red")

    assert _needs_a_person(fakes, result)
    assert "checks still failing" in result.summary
    assert len(fakes.of(OPEN_CHANGE_SET)) == 1


async def test_a_pr_conflicting_again_after_the_merges_ends_the_run_needing_a_person() -> None:
    fakes = FakeActivities(merges=True, ci_conflicts=MAX_BASE_SYNCS + 1)
    result, _ = await run_standard(fakes, "run-sync-ci-cap")

    assert _needs_a_person(fakes, result)
    assert [a for a, _ in _syncs(fakes)] == list(range(1, MAX_BASE_SYNCS + 1))
    assert len(fakes.of(WAIT_FOR_CI)) == MAX_BASE_SYNCS + 1
    assert "conflicting with the base branch" in result.summary
    assert fakes.of(MERGE_CHANGE_SET) == []


async def test_an_out_of_date_merge_merges_the_base_and_tries_again() -> None:
    fakes = FakeActivities(merges=True, merge_script=["out_of_date"], sync_script=["merged"])
    result, _ = await run_standard(fakes, "run-sync-out-of-date")

    assert result.outcome == RunOutcome.MERGED
    assert _syncs(fakes) == [(1, False)]
    assert len(fakes.of(MERGE_CHANGE_SET)) == TWICE
    assert len(fakes.of(OPEN_CHANGE_SET)) == TWICE
    assert len(fakes.of(WAIT_FOR_CI)) == TWICE
    ready: list[NotifyInput] = [n for n in fakes.of(NOTIFY) if n.kind == NotificationKind.PRS_READY]
    assert ready == []


async def test_unresolved_conflicts_on_an_out_of_date_merge_end_the_run_needing_a_person() -> None:
    fakes = FakeActivities(
        merge_script=["out_of_date"], sync_script=["conflicted"] * 3, resolve_outcome=StepOutcome.NEEDS_HUMAN
    )
    result, _ = await run_standard(fakes, "run-sync-merge-stuck")

    assert _needs_a_person(fakes, result)


async def test_a_merge_still_out_of_date_after_the_syncs_waits_for_a_person() -> None:
    fakes = FakeActivities(merge_script=["out_of_date"] * (MAX_BASE_SYNCS + 1))
    result, _ = await run_standard(fakes, "run-sync-give-up")

    assert result.outcome == RunOutcome.MERGED  # the person merged it
    assert len(fakes.of(MERGE_CHANGE_SET)) == MAX_BASE_SYNCS + 1
    assert [a for a, _ in _syncs(fakes)] == list(range(1, MAX_BASE_SYNCS + 1))
    assert next(n.kind for n in fakes.of(NOTIFY)) == NotificationKind.PRS_READY
