---
id: M2-14
title: "Usage-aware scheduler and retire scripts/task_loop.py"
type: feature
complexity: standard
status: planned
depends_on: [M2-07, M2-13]
human_actions:
  - "Check whether a long-lived token from `claude setup-token` is accepted by the usage endpoint (`fetch_claude_usage`); if it is, put it in `.env` as FACT_CLAUDE__USAGE_TOKEN. If not, say so: fact then reads the Claude Code CLI login, else the windows recorded from runs."
---

## Goal

The scheduler covers everything `scripts/task_loop.py` did that's worth keeping, so the script and its helpers are deleted. Usage limits are handled from M2-13. This task adds the rest: the scheduler starts nothing while a Claude usage window is full, stops restarting a task that keeps failing, previews what it would start next, and keeps the machine awake while runs are active.

## Read first

- `CLAUDE.md`
- `docs/spec/10-observability.md` ("Capacity and monitors")
- `core/src/fact/core/claude_usage.py` (`fetch_claude_usage`, `read_cli_token`, `ClaudeUsage.blocked`)
- The `SchedulerTick` workflow and `find_ready_tasks` activity from M2-07 (`worker/src/fact/worker/`)
- `scripts/task_loop.py` (`keep_awake`, `wait_for_quota`: the behaviour being replaced)
- `.agents/skills/next-task/SKILL.md` and `next_task.py` (the `resume` handling to remove)

## Do

- **Usage gate.** Settings: `claude.usage_token` (secret, `FACT_CLAUDE__USAGE_TOKEN`) and `concurrency.usage_threshold` (default 0.9). A `claude_usage` control activity reads usage from:
  1. `fetch_claude_usage` with `claude.usage_token`, else with `read_cli_token()`;
  2. if the endpoint is unavailable, the windows from `GET /api/v1/usage` (M2-13) whose `resets_at` is still in the future.
  `SchedulerTick` calls it before starting anything. When `ClaudeUsage.blocked(threshold)` isn't empty, the tick starts no run and logs which window is full and when it resets. Running steps are not touched; M2-13 handles them. The endpoint is called at most once per tick, and not at all when no task is ready.
- **Failure cap.** Setting `concurrency.max_failed_runs` (default 3). `find_ready_tasks` skips a task whose last N runs all ended `failed` with no `passed` or `merged` run since. It's reported as needing a person until a new run is started by hand (`fact run`), which resets the count.
- **Preview.** `fact scheduler status` shows, along with the Schedule state:
  - the ready tasks in the order the next tick would start them;
  - the reason each one waits, if any: dependency, cap, usage window (with reset time) or failure cap;
  - the current usage windows.
  Serve it from an API endpoint that reuses `find_ready_tasks`'s ordering, not a copy of it.
- **Keep awake.** On Windows, while at least one run is queued, running or waiting, the worker holds `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` and releases it when none are. The hold only lasts while the calling thread lives, so call it from one long-lived thread (the main or event-loop thread), never through `asyncio.to_thread` or an executor pool. It's a no-op elsewhere. Setting `worker.keep_awake` (default true).
- **Retire the loop.**
  - Delete `scripts/task_loop.py`, `loop_recovery.py`, `loop_report.py`, `claude_run.py`, `claude_log.py` and `task_files.py`, plus `scripts/tests/test_loop_recovery.py`, `test_loop_report.py`, and anything in `conftest.py` that only served them. Keep `test_project_dot_agents.py` passing.
  - Remove the task-loop entries from `.vscode/launch.json`.
  - Remove the `resume` field and `## Resume` handling from `tasks/README.md`, `.agents/skills/next-task/SKILL.md` and `next_task.py`.
  - Remove `.tmp/task-loop` mentions.
  - Update `AGENTS.md` (development commands: how to run every ready task is `fact dev` plus `fact scheduler on`), `docs/ARCHITECTURE.md`, and the backlog (drop the "Usage-aware scheduling" and "Task loop recovery" items).
- Tests:
  - the tick starts nothing when a window is over the threshold, and starts as usual once its `resets_at` has passed (time-skipping, mocked usage);
  - the stored-window fallback when the endpoint raises `UsageUnavailableError`;
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
- [ ] With a usage window mocked over the threshold, the scheduler starts no run, and `fact scheduler status` shows the window and its reset time (test).
- [ ] A task whose last 3 runs failed isn't started again by the scheduler, and `fact scheduler status` says why (test).
- [ ] On Samuel's machine, `fact dev` plus `fact scheduler on` with one `ready` task starts it within a minute and the PC doesn't sleep during the run (checked by hand, noted in the PR).
