---
id: M2-14
title: "Usage-aware scheduler and retire scripts/task_loop.py"
type: feature
complexity: complex
status: planned
depends_on: [M2-07, M2-13]
human_actions: []
---

## Goal

The scheduler covers everything `scripts/task_loop.py` did that's worth keeping, so the script and its helpers are deleted. Usage limits are handled from M2-13. This task adds the rest: the scheduler starts nothing while a Claude usage window is full, stops restarting a task that keeps failing, previews what it would start next, and keeps the machine awake while runs are active.

## Read first

- `CLAUDE.md`
- `docs/spec/10-observability.md` ("Capacity and monitors")
- `core/src/fact/core/claude_usage.py` (`fetch_claude_usage`, `read_cli_token`, `ClaudeUsage.blocked`)
- `sdk/src/fact/sdk/agents.py` (M2-13's limit pause and `MAX_LIMIT_PAUSES`)
- The `SchedulerTick` workflow and `find_ready_tasks` activity from M2-07 (`worker/src/fact/worker/`)
- `scripts/task_loop.py` (`keep_awake`, `current_usage`, `wait_for_quota`: the behaviour being replaced)
- `.agents/skills/next-task/` and `.agents/skills/plan-tasks/` (to delete)

## Do

- **Usage gate.** A start gate for runs, nothing more: runs already going are never touched (M2-13 pauses them at the hard limit), and the gate only decides whether the scheduler starts a new one.
  - **When it applies.** Setting `claude.usage_gate` (default true). Off when `ANTHROPIC_API_KEY` is set in the worker's environment: that key wins over the login and bills the API, so subscription windows don't matter. Every route uses the `claude-code` harness today; a per-route check comes with a second harness.
  - **Reserves.** Settings `claude.reserve.five_hour` (default 0.10) and `claude.reserve.weekly` (default 0.05, applied to `seven_day`, `seven_day_opus` and `seven_day_sonnet`): fact leaves that share of each window to the person. A window is full when its utilization is at or above `1 - reserve`, or it is rejecting. This replaces a single threshold; add a `ClaudeUsage` helper taking per-window thresholds rather than calling `blocked(threshold)` per group.
  - **Credentials: local only.** The `claude_usage` control activity calls `fetch_claude_usage(read_cli_token())`, the token Claude Code stores under `~/.claude` (`CLAUDE_CONFIG_DIR`), as `scripts/task_loop.py` does. No token setting: a `claude setup-token` token is refused by the usage endpoint (403, checked by hand 2026-11-26). Remote login and fact refreshing its own OAuth token are the backlog item "Claude credentials for sessions and usage".
  - **No polling.** The endpoint is undocumented; call it only when needed, as the loop's `current_usage` does. Keep the last answer in the worker process. A tick calls the endpoint only when a task is ready and a run slot is free, and:
    - when the last answer has a full window whose `resets_at` is still ahead, the tick starts nothing and does not call;
    - once that `resets_at` (plus a margin) has passed, it calls once to confirm the reset; if the window is still full past its reset, it backs off before the next call (growing waits, as `wait_for_quota`'s `MIN_WAIT_S * stalls`);
    - otherwise it calls once before starting runs, so usage spent outside fact is seen.
    A worker restart drops the cached answer, which costs one call. The scheduler never reads the windows runs record (M2-13's `usage_windows`); those stay for the in-run pause.
  - **Full window.** The tick starts no run and logs which window is full and when it resets.
  - **Usage unreadable** (no credentials file, expired token, endpoint error or unexpected answer): an error, not a block. Log it at error level, show it in the preview, and start runs as usual: a run's own Claude session refreshes the CLI token, and M2-13's guardrails hold a run the account refuses. Check those guardrails and add what's missing: runs paused on the limit count as `waiting` against `concurrency.max_runs` and each project's `max_runs`, so the scheduler can't pile up paused runs, and a step ends `needs_human` after `MAX_LIMIT_PAUSES`.
- **Failure cap.** Setting `concurrency.max_failed_runs` (default 3). `find_ready_tasks` skips a task whose last N runs all ended `failed` with no `passed` or `merged` run since. It's reported as needing a person until a new run is started by hand (`fact run`), which resets the count.
- **Preview.** `fact scheduler status` shows, along with the Schedule state:
  - the ready tasks in the order the next tick would start them;
  - the reason each one waits, if any: dependency, cap, usage window (with reset time) or failure cap;
  - the last usage answer: each window's utilization, its reserve and reset time, or the error when usage is unreadable. The preview reads the cached answer and never calls the endpoint itself.
  Serve it from an API endpoint that reuses `find_ready_tasks`'s ordering, not a copy of it.
- **Keep awake.** On Windows, while at least one run is queued, running or waiting, the worker holds `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` and releases it when none are. The hold only lasts while the calling thread lives, so call it from one long-lived thread (the main or event-loop thread), never through `asyncio.to_thread` or an executor pool. It's a no-op elsewhere. Setting `worker.keep_awake` (default true).
- **Retire the loop.**
  - Delete `scripts/task_loop.py`, `loop_recovery.py`, `loop_report.py`, `claude_run.py`, `claude_log.py` and `task_files.py`, plus `scripts/tests/test_loop_recovery.py`, `test_loop_report.py`, and anything in `conftest.py` that only served them. Keep `test_project_dot_agents.py` passing.
  - Remove the task-loop entries from `.vscode/launch.json`.
  - Delete the file-task skills `.agents/skills/next-task/` (with `next_task.py`) and `.agents/skills/plan-tasks/`: fact's work is GitHub issues now (M2-12), planned by hand on GitHub until M5's planner. Remove their mentions (`AGENTS.md` "If a run goes wrong", `docs/spec/15-milestones.md` M5, `tasks/backlog.md` M5 line).
  - This task's file is the last one in `tasks/`. Remove the `resume` field from `tasks/README.md`, keep the format there for projects with `task_source: files`, and keep `tasks/backlog.md`. `test_real_tasks_folder_is_valid` must still pass on a folder with no task files.
  - Remove `.tmp/task-loop` mentions.
  - Update `AGENTS.md` (development commands: how to run every ready task is `fact dev` plus `fact scheduler on`), `docs/ARCHITECTURE.md`, and the backlog (drop the "Usage-aware scheduling" and "Task loop recovery" items).
- Tests:
  - the tick starts nothing when a window is inside its reserve, makes no call while that `resets_at` is ahead, makes one call once it has passed and starts as usual when the reset is confirmed (time-skipping, mocked usage);
  - per-window reserves: five-hour at 0.90 blocks, weekly at 0.94 doesn't, weekly at 0.95 does;
  - no endpoint call when no task is ready or no slot is free; one call per tick otherwise;
  - backoff when a window still reads full past its reset;
  - unreadable usage (`UsageUnavailableError`) logs an error and the tick still starts runs within the caps;
  - the gate is off with `claude.usage_gate: false` and with `ANTHROPIC_API_KEY` set;
  - the failure cap and its reset by a manual run;
  - the preview's order and reasons match what the tick does;
  - keep-awake is held and released (mock `ctypes`, skipped off Windows).

## Out of scope

- Re-verifying work from runs that hit the limit (dropped: the review stage and CI check the PR).
- A blocked-task report (dropped: `needs_human` runs and notifications from M2-03 cover it).
- A `--max-tasks` equivalent (dropped: `fact scheduler off`).
- Budgets in USD and the Costs view (M6).

## Acceptance criteria

- [ ] `uv run pytest` passes.
- [ ] `scripts/` holds no task-loop code and `grep -r task_loop` outside `.git` finds nothing.
- [ ] `.agents/skills/` has no `next-task` or `plan-tasks`, and `grep -rE "next-task|plan-tasks"` outside `.git` finds only history (`docs/decisions*.md`).
- [ ] With a usage window mocked inside its reserve, the scheduler starts no run and makes no endpoint call until the window's reset time, and `fact scheduler status` shows the window, its reserve and its reset time (test).
- [ ] With usage unreadable, the scheduler logs an error and still starts runs, at most `concurrency.max_runs` of them, paused runs included (test).
- [ ] A task whose last 3 runs failed isn't started again by the scheduler, and `fact scheduler status` says why (test).
- [ ] On Samuel's machine, `fact dev` plus `fact scheduler on` with one `ready` task starts it within a minute and the PC doesn't sleep during the run (checked by hand, noted in the PR).
