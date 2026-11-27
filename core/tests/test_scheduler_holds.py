"""`plan` reasons, `run_history_hold` (the failure cap), per-window reserves, and `list_ready(include_waiting=True)`."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fact.core.claude_usage import ClaudeUsage, UsageWindow
from fact.core.enums import RunOutcome, RunStatus, RunTrigger
from fact.core.scheduler import Hold, ProjectLoad, ReadyTask, ReadyTasks, RunRecord, plan, plan_starts, run_history_hold
from fact.core.settings import ClaudeSettings
from fact.core.tasks.files import FileTaskSource
from fact.core.usage_gate import GateState, UsageCheck, full_reason, gate_windows

RESET = datetime(2026, 11, 26, 18, 0, tzinfo=UTC)
FAILED = RunRecord(status=RunStatus.FINISHED, outcome=RunOutcome.FAILED, trigger=RunTrigger.SCHEDULER)
FAILED_BY_HAND = RunRecord(status=RunStatus.FINISHED, outcome=RunOutcome.FAILED, trigger=RunTrigger.MANUAL)


def _task(task_id: str, **extra: object) -> ReadyTask:
    return ReadyTask(project="a", task_key=f"task:{task_id}", task_id=task_id, **extra)


def _ready(tasks: list[ReadyTask], max_runs: int = 2) -> ReadyTasks:
    return ReadyTasks(tasks=tasks, projects=[ProjectLoad(project="a", active=0, max_runs=5)], max_runs=max_runs)


def test_plan_gives_each_task_its_reason() -> None:
    tasks = [
        _task("M1-01", hold=Hold.DEPENDENCY, waiting_on=["task:M0-09"]),
        _task("M1-02", hold=Hold.FAILURE_CAP, failed_runs=3),
        _task("M1-03"),
        _task("M1-04"),
        _task("M1-05"),
    ]
    got = [(p.task.task_id, p.hold) for p in plan(_ready(tasks))]
    assert got == [
        ("M1-01", Hold.DEPENDENCY),
        ("M1-02", Hold.FAILURE_CAP),
        ("M1-03", None),
        ("M1-04", None),
        ("M1-05", Hold.CAP),
    ]
    planned = plan(_ready(tasks))
    assert "task:M0-09" in planned[0].detail
    assert "fact run" in planned[1].detail
    assert [t.task_id for t in plan_starts(_ready(tasks))] == ["M1-03", "M1-04"]


def test_usage_holds_only_what_the_caps_let_start() -> None:
    planned = plan(_ready([_task("M1-01"), _task("M1-02"), _task("M1-03")]), usage_full="five_hour 95%")
    assert [(p.hold, p.detail) for p in planned] == [
        (Hold.USAGE, "five_hour 95%"),
        (Hold.USAGE, "five_hour 95%"),
        (Hold.CAP, "max_runs 2 reached"),
    ]


@pytest.mark.parametrize(
    ("history", "expected"),
    [
        ([], (True, 0, None)),
        ([RunRecord(status=RunStatus.RUNNING)], (False, 0, None)),
        ([RunRecord(status=RunStatus.WAITING), FAILED], (False, 0, None)),
        ([RunRecord(status=RunStatus.FINISHED, outcome=RunOutcome.NEEDS_HUMAN)], (False, 0, None)),
        ([FAILED, FAILED], (True, 2, None)),
        ([FAILED, FAILED, FAILED], (True, 3, Hold.FAILURE_CAP)),
        # a merged run (then reopened) ends the count
        ([FAILED, FAILED, RunRecord(status=RunStatus.FINISHED, outcome=RunOutcome.MERGED), FAILED], (True, 2, None)),
        # a run a person started resets the count: it counts itself, nothing older
        ([FAILED, FAILED_BY_HAND, FAILED, FAILED], (True, 2, None)),
        ([FAILED_BY_HAND, FAILED, FAILED, FAILED], (True, 1, None)),
        # an older run still in progress behind a failed manual one: never a second run beside it
        ([FAILED_BY_HAND, RunRecord(status=RunStatus.WAITING)], (False, 0, None)),
    ],
)
def test_run_history_hold(history: list[RunRecord], expected: tuple[bool, int, Hold | None]) -> None:
    assert run_history_hold(history, max_failed=3) == expected


@pytest.mark.parametrize(
    ("history", "expected"),
    [
        # an issue's label holds it: a run end that is not `failed` holds nothing in its history
        ([RunRecord(status=RunStatus.FINISHED, outcome=RunOutcome.STOPPED), FAILED], (True, 0, None)),
        ([FAILED, FAILED, FAILED], (True, 3, Hold.FAILURE_CAP)),
        ([RunRecord(status=RunStatus.RUNNING)], (False, 0, None)),
    ],
)
def test_run_history_holds_an_issue_only_by_a_run_in_progress_or_failures(
    history: list[RunRecord], expected: tuple[bool, int, Hold | None]
) -> None:
    assert run_history_hold(history, max_failed=3, labelled=True) == expected


def _usage(**utilization: float) -> ClaudeUsage:
    return ClaudeUsage(
        windows=tuple(UsageWindow(name=n, utilization=u, resets_at=RESET) for n, u in utilization.items())
    )


@pytest.mark.parametrize(
    ("utilization", "full"),
    [
        ({"five_hour": 0.89, "seven_day": 0.5}, []),
        ({"five_hour": 0.90}, ["five_hour"]),
        ({"seven_day": 0.94, "seven_day_opus": 0.94, "seven_day_sonnet": 0.94}, []),
        ({"seven_day": 0.95}, ["seven_day"]),
        ({"seven_day_sonnet": 0.95, "five_hour": 0.2}, ["seven_day_sonnet"]),
    ],
)
def test_per_window_reserves(utilization: dict[str, float], full: list[str]) -> None:
    windows = gate_windows(_usage(**utilization), ClaudeSettings())
    assert [w.name for w in windows if w.full] == full


def test_rejecting_window_is_full_and_reasons_name_it() -> None:
    usage = ClaudeUsage(windows=(UsageWindow(name="five_hour", utilization=0.1, resets_at=RESET, rejected=True),))
    windows = gate_windows(usage, ClaudeSettings())
    assert windows[0].full
    assert full_reason(windows) == "five_hour 10% (reserve 10%) resets 2026-11-26 18:00 UTC"


def test_usage_check_held_until_next_call() -> None:
    check = UsageCheck(state=GateState.FULL, next_call_at=RESET)
    assert check.held(RESET - timedelta(seconds=1))
    assert not check.held(RESET)
    assert not UsageCheck(state=GateState.UNAVAILABLE, error="x").held(RESET)


def _write(root: Path, rel: str, task_id: str, deps: list[str], status: str = "ready") -> None:
    path = root / "tasks" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    front = f"---\nid: {task_id}\ntitle: t\nstatus: {status}\ndepends_on: [{', '.join(deps)}]\n---\n"
    path.write_text(front, encoding="utf-8")


async def test_file_source_lists_waiting_tasks(tmp_path: Path) -> None:
    _write(tmp_path, "M1/02-a.md", "M1-02", ["M1-01"])  # M1-01 finished: its file is gone
    _write(tmp_path, "M1/03-b.md", "M1-03", ["M1-02"])
    _write(tmp_path, "M1/04-c.md", "M1-04", [], status="planned")
    source = FileTaskSource(tmp_path)
    assert [(i.task.key, i.waiting_on) for i in await source.list_ready()] == [("task:M1-02", ())]
    waiting = await source.list_ready(include_waiting=True)
    assert sorted((i.task.key, i.waiting_on) for i in waiting) == [
        ("task:M1-02", ()),
        ("task:M1-03", ("task:M1-02",)),
    ]


def test_a_task_under_a_parent_not_ready_waits_for_it() -> None:
    gated = ReadyTask(project="a", task_key="gh:acme/app#21", task_id="21", hold=Hold.PARENT, gated_by="gh:acme/app#20")
    [planned] = plan(_ready([gated]))
    assert planned.hold == Hold.PARENT
    assert planned.detail == "waits for its ancestor gh:acme/app#20 to be marked ready"
