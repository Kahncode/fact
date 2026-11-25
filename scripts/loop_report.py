"""What task_loop.py prints when it stops: why it failed, and what a person must do to unblock the remaining tasks."""

# ruff: noqa: INP001, T201, DTZ005
from datetime import datetime
from pathlib import Path

from loop_recovery import STATE_DIR, git
from task_files import TASKS, Task, load_tasks


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def blocker_report(blocked: dict[str, str], tasks_dir: Path = TASKS) -> str:
    """
    List the tasks that need a person: those a run reported BLOCKED this session, and those with human_actions.

    Under each one, the tasks waiting on it. Empty when nothing is blocked.
    """
    tasks = {k: t for k, t in load_tasks(tasks_dir).items() if t.status != "done"}
    needs: dict[str, list[str]] = {tid: [f"agent: {why}"] for tid, why in blocked.items() if tid in tasks}
    for t in tasks.values():
        needs.setdefault(t.id, []).extend(f"human action: {a}" for a in t.human_actions)
    needs = {tid: items for tid, items in needs.items() if items}
    lines = []
    for tid, items in needs.items():
        lines.append(f"{tid} ({tasks[tid].path.name})")
        lines.extend(f"    - {item}" for item in items)
        if waiting := waiting_on(tid, tasks):
            lines.append(f"    holds up: {', '.join(waiting)}")
    return "\n".join(lines)


def waiting_on(task_id: str, tasks: dict[str, Task]) -> list[str]:
    """Every open task that depends on `task_id`, directly or through other open tasks."""
    found: list[str] = []
    frontier = [task_id]
    while frontier:
        current = frontier.pop()
        for t in tasks.values():
            if current in t.depends_on and t.id not in found:
                found.append(t.id)
                frontier.append(t.id)
    return sorted(found)


def print_blockers(blocked: dict[str, str]) -> None:
    if report := blocker_report(blocked):
        log("NEEDS YOU: these tasks are blocked until a person acts\n" + report)


def stop_with(report: str, blocked: dict[str, str], headline: str = "task did not complete") -> int:
    """Print why the loop stops (also saved to FAILED.txt) and the blockers, then return the exit code."""
    log(f"STOPPING: {headline}\n{report}")
    log(f"working tree: {git('status', '--short') or 'clean'}")
    (STATE_DIR / "FAILED.txt").write_text(report + "\n", encoding="utf-8")
    print_blockers(blocked)
    return 1
