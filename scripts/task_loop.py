"""
Run `/next-task` repeatedly until no runnable task is left or the Claude quota is used up.

    uv run python scripts/task_loop.py                  # run until done, wait out 5h-window resets
    uv run python scripts/task_loop.py --dry-run        # list what would run, in order
    uv run python scripts/task_loop.py --threshold 0.8  # pause earlier (default 0.9)
    uv run python scripts/task_loop.py --max-tasks 5

Anthropic is called as little as possible: the usage windows come from the `rate_limit_event`s of the
task runs themselves, and the usage endpoint (else a tiny Haiku run) is probed only once at start and
again when every known window has reached its reset time. A task is run only while the 5h and 7d
windows are below the threshold. When the 5h window is full the loop sleeps until it resets; when the 7d window
is full (or the wait would exceed --max-wait-hours) it stops.

A task whose run hit the usage limit is reopened and verified by a second run, and a crash or restart (even during
the wait for a reset) resumes the task in progress on top of its uncommitted work: see loop_recovery.py.

A run ending in `BLOCKED: <why>` is not retried; the task is skipped this session. At the end the loop prints what a
person must do: those reasons and the human_actions of the open tasks.

The loop stops when a task fails --max-attempts times (the report is printed and saved to
`.tmp/task-loop/FAILED.txt`), when the tree is dirty with work it can't attribute to the top task, or when
`.tmp/task-loop/STOP` exists. Every run is logged to `.tmp/task-loop/logs/` (git-ignored): `<task>-<time>.log` is a
readable transcript (tail it to follow a run), `.jsonl` the raw stream-json events.
"""

# ruff: noqa: INP001, DTZ005
import argparse
import ctypes
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta

import loop_recovery
from claude_run import run_claude
from fact.core.claude_usage import ClaudeUsage, UsageUnavailableError, fetch_claude_usage, read_cli_token
from loop_recovery import (
    RESUME_NOTE,
    ROOT,
    LoopState,
    Recovery,
    decide,
    git,
    reopen_task,
    restore_task_file,
    tree_is_clean,
)
from loop_report import log, print_blockers, stop_with
from task_files import Task, runnable_tasks

STATE = loop_recovery.STATE_DIR
STOP_FILE = STATE / "STOP"
RESET_MARGIN_S = 90
MIN_WAIT_S = 300  # never re-check faster than this (times the stall count), even when the reset is past
HEARTBEAT_S = 600  # log a "still waiting" line this often while sleeping
MAX_STALLS = 10  # consecutive probes that still say "blocked" with a reset in the past, then stop
MAX_PAUSES = 20  # rate-limit pauses per task, a guard against a misdetected limit
MAX_REOPENS = 3  # verification runs per task that hit the limit again, then stop


def keep_awake() -> None:
    """Stop Windows sleeping while the loop runs (no-op elsewhere; use caffeinate/systemd-inhibit there)."""
    if sys.platform == "win32":
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED


# --- usage ---------------------------------------------------------------------------------------


def probe_usage() -> ClaudeUsage:
    """Read usage from the endpoint (FACT_CLAUDE_OAUTH_TOKEN, else the CLI's token), else from a tiny Haiku run."""
    try:
        return fetch_claude_usage(os.environ.get("FACT_CLAUDE_OAUTH_TOKEN") or read_cli_token())
    except UsageUnavailableError as exc:
        log(f"usage endpoint unavailable ({exc}); probing with haiku")
    return run_claude("ok", "haiku", "", None).usage


_known = ClaudeUsage()  # last usage we heard from Anthropic: probes and the events of our own task runs


def remember_usage(usage: ClaudeUsage) -> None:
    global _known  # noqa: PLW0603
    _known = _known.merge(usage)


def current_usage() -> ClaudeUsage:
    """
    Last known usage without calling Anthropic. Windows past their reset are dropped (they are empty now).

    Only when nothing usable is left (start of the run, or every known window has reset) does it probe.
    """
    global _known  # noqa: PLW0603
    now = datetime.now(UTC)
    live = tuple(w for w in _known.windows if w.resets_at > now)
    if live:
        _known = ClaudeUsage(windows=live)
    else:
        log("no live usage data; probing once")
        _known = probe_usage()
    return _known


def wait_for_quota(threshold: float, max_wait_s: float) -> bool:
    """Block until all windows are below the threshold. False if we should stop instead."""
    stalls = 0
    while True:
        usage = current_usage()
        resume_at = usage.resume_at(threshold, timedelta(seconds=RESET_MARGIN_S))
        if resume_at is None:
            log(f"usage ok: {usage.summary()}")
            return True
        wait = (resume_at - datetime.now(UTC)).total_seconds()
        log(f"usage high: {usage.summary()} (blocked: {', '.join(w.name for w in usage.blocked(threshold))})")
        for w in usage.blocked(threshold):
            log(f"  {w.name}: {w.utilization:.0%} used, resets at {w.resets_at.astimezone():%Y-%m-%d %H:%M}")
        if wait > max_wait_s:
            log(f"reset is {wait / 3600:.1f}h away, over the wait limit; stopping")
            return False
        if wait < MIN_WAIT_S:  # reset is (almost) due: re-check, backing off if it keeps saying blocked
            stalls += 1
            if stalls > MAX_STALLS:
                log(f"still blocked after {MAX_STALLS} checks past the reported reset; stopping")
                return False
            wait = MIN_WAIT_S * stalls
        else:
            stalls = 0
        log(f"sleeping until {(datetime.now(UTC) + timedelta(seconds=wait)).astimezone():%H:%M} ({wait / 60:.0f} min)")
        sleep_interruptible(wait)
        if STOP_FILE.exists():
            return False


def sleep_interruptible(seconds: float) -> None:
    end = time.time() + seconds
    next_beat = time.time() + HEARTBEAT_S
    while time.time() < end and not STOP_FILE.exists():
        time.sleep(min(30, max(0, end - time.time())))
        if time.time() >= next_beat and time.time() < end:
            log(f"still waiting for the usage reset: {(end - time.time()) / 60:.0f} min left")
            next_beat = time.time() + HEARTBEAT_S


# --- main loop -----------------------------------------------------------------------------------


def state_for(task: Task) -> LoopState:
    """Return the recorded state if it is this task's and nothing landed since (a resume), else a fresh one."""
    head = git("rev-parse", "HEAD")
    state = LoopState.load()
    if state and state.task_id == task.id and state.head_before == head:
        return state
    return LoopState(task.id, task.path.relative_to(ROOT).as_posix(), head)


def run_task(task: Task, args: argparse.Namespace, state: LoopState, blocked: dict[str, str]) -> str | None:
    """
    Run one task until it commits or reports BLOCKED (recorded in `blocked`). Else the report of why the loop stops.

    Rate-limit pauses are not failures. Any other unsuccessful attempt counts, and the task gets
    --max-attempts of them. A run that starts on a dirty tree is told to continue from that work.
    """
    model = "opus" if task.complexity == "complex" else "sonnet"
    failures, pauses, reasons = 0, 0, []
    while failures < args.max_attempts:
        if restore_task_file(state):
            log(f"{task.id}: restored the task file the last run deleted without committing")
        resuming = not tree_is_clean()
        log(f"{task.id}: attempt {failures + 1}/{args.max_attempts}, model {model}{', resuming' if resuming else ''}")
        log_path = STATE / "logs" / f"{task.id}-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
        log(f"{task.id}: transcript {log_path.with_suffix('.log')}")
        state.save()
        result = run_claude(
            f"/next-task {task.id}",
            model,
            args.permission_mode,
            log_path,
            unattended=True,
            note=RESUME_NOTE if resuming else "",
        )
        remember_usage(result.usage)
        if result.hit_limit:
            state.hit_limit = True
            state.save()
        log(f"{task.id}: exit {result.returncode}, cost ${result.cost:.2f}, usage {result.usage.summary()}")
        if not task.path.exists() and git("rev-parse", "HEAD") != state.head_before and tree_is_clean():  # landed
            return None
        if result.rate_limited and pauses < MAX_PAUSES:
            pauses += 1
            log(f"{task.id}: hit the usage limit mid-task; waiting for reset, then continuing")
            if not wait_for_quota(args.threshold, args.max_wait_hours * 3600):
                return f"{task.id}: usage limit reached and the reset is too far away (partial work is in the tree)"
            continue
        if result.blocker:
            blocked[task.id] = result.blocker
            log(f"{task.id}: BLOCKED: {result.blocker}")
            return None
        failures += 1
        reasons.append(f"attempt {failures}: {result.failure_reason()} (log: {log_path})")
        log(f"{task.id}: attempt {failures} failed: {result.failure_reason()}")
    return f"{task.id}: failed {failures} times\n  " + "\n  ".join(reasons)


def settle(task: Task, state: LoopState, blocked: dict[str, str]) -> str | None:
    """After a run that landed or reported BLOCKED: reopen an unverified commit, else clear the state. Or a report."""
    if task.id in blocked and not tree_is_clean():
        return f"{task.id}: BLOCKED with uncommitted work in the tree"
    if task.id not in blocked and state.hit_limit:
        return reopen(state)
    LoopState.clear()
    return None


def reopen(state: LoopState) -> str | None:
    """Reopen the task and record the reopen in the state (it survives restarts). A report if the loop must stop."""
    if state.reopens >= MAX_REOPENS:
        LoopState.clear()  # a restart must not reopen it again
        return f"{state.task_id}: hit the usage limit in {MAX_REOPENS} verification runs in a row; its commits are unverified"
    try:
        path = reopen_task(state.task_path, state.head_before)
    except subprocess.CalledProcessError as exc:
        return f"{state.task_id}: committing the reopened task file failed (it is staged): {exc.stderr or exc.stdout}"
    LoopState(state.task_id, state.task_path, git("rev-parse", "HEAD"), reopens=state.reopens + 1).save()
    log(f"{state.task_id}: a run hit the usage limit, so its commits are unverified; reopened {path.name} to run again")
    return None


def recover() -> str | None:
    """Deal with what the previous loop left behind (see loop_recovery.py). A report if the loop must not start."""
    state = LoopState.load()
    if state and restore_task_file(state):
        log(f"{state.task_id}: restored the task file the interrupted run deleted without committing")
    candidates = runnable_tasks()
    action = decide(
        state,
        head=git("rev-parse", "HEAD"),
        clean=tree_is_clean(),
        task_file_exists=bool(state and (ROOT / state.task_path).exists()),
        is_top_task=bool(state and candidates and candidates[0].id == state.task_id),
    )
    if action is Recovery.DIRTY:
        return "working tree is not clean and holds no resumable task run; commit or stash first"
    assert state is not None or action is Recovery.NONE  # noqa: S101
    if action is Recovery.REOPEN:
        return reopen(state)
    if action is Recovery.RESUME:
        log(f"{state.task_id}: resuming the interrupted run on top of the work in the tree")
    elif state and not (ROOT / state.task_path).exists():
        LoopState.clear()  # the task landed; a reopened task that is still there keeps its reopen count
    return None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--threshold", type=float, default=0.9, help="pause when a usage window reaches this (0..1)")
    p.add_argument("--max-wait-hours", type=float, default=6, help="stop instead of waiting longer than this")
    p.add_argument("--max-tasks", type=int, default=0, help="stop after N tasks (0 = no limit)")
    p.add_argument("--max-attempts", type=int, default=5, help="failed attempts per task before the loop stops")
    p.add_argument("--permission-mode", default="bypassPermissions", help="claude --permission-mode ('' to omit)")
    p.add_argument("--dry-run", action="store_true", help="print the runnable tasks and exit")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.dry_run:
        for t in runnable_tasks():
            log(f"{t.id} ({t.complexity}) {t.path.relative_to(ROOT).as_posix()} {t.resume and 'verify ' + t.resume}")
        return 0
    (STATE / "logs").mkdir(parents=True, exist_ok=True)
    STOP_FILE.unlink(missing_ok=True)
    blocked: dict[str, str] = {}  # task id -> what the agent said a person must do
    if report := recover():
        return stop_with(report, blocked, "cannot start")
    keep_awake()
    done = 0
    while not STOP_FILE.exists() and (not args.max_tasks or done < args.max_tasks):
        candidates = [t for t in runnable_tasks() if t.id not in blocked]
        if not candidates:
            log("no runnable task left")
            print_blockers(blocked)
            break
        if not wait_for_quota(args.threshold, args.max_wait_hours * 3600):
            break
        task = candidates[0]
        state = state_for(task)
        if report := run_task(task, args, state, blocked) or settle(task, state, blocked):
            return stop_with(report, blocked)
        if task.id not in blocked and not state.hit_limit:
            done += 1
            log(f"{task.id}: committed ({done} done this session)")
    log(f"finished: {done} task(s) completed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
