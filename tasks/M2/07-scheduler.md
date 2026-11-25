---
id: M2-07
title: "Scheduler: start ready tasks in dependency order"
type: feature
complexity: standard
status: planned
depends_on: [M2-06]
human_actions: []
---

## Goal

fact picks up work by itself. A Temporal Schedule starts a short `SchedulerTick` workflow every 30 s. Each tick finds file tasks with `status: ready` and every dependency done on the default branch, and starts runs for them in dependency order within the concurrency caps. No other cron or background job exists in fact. Planning in `tasks/` then flipping a status is enough to get work done.

## Read first

- `CLAUDE.md`
- `docs/spec/08-planning.md` ("Dependencies drive scheduling")
- `docs/spec/10-observability.md` (Capacity and monitors: caps)
- `core/src/fact/core/tasks/files.py`
- `api/src/fact/api/routers/runs.py` (`start`)
- Temporal Python docs on Schedules (`client.create_schedule`, `ScheduleOverlapPolicy`)

## Do

- A `SchedulerTick` workflow on `control`, short-lived, that:
  1. calls a `find_ready_tasks` activity: per project with a files task source, `git fetch`, then read the tasks from `origin/<default>` and keep those that are `ready`, have all dependencies `done`, and have no active run and no run that ended `merged`;
  2. orders them by dependency depth, then id;
  3. starts as many as the caps allow (through the same code path as `POST /runs/start`): `concurrency.max_runs` (add it, default 2) overall, and `max_runs` per project (default 2), counting runs that are queued, running or waiting.
- A Temporal Schedule with id `fact-scheduler`: every 30 s, overlap policy `SKIP`, so a slow tick never stacks.
  - The worker creates it on startup if it's missing and updates its interval if `fact.yaml` changed.
  - It's created paused unless `concurrency.scheduler_enabled` is true.
- `fact scheduler on|off|status` pauses, unpauses and describes the Schedule through the API. Pausing from the Temporal UI works too.
- Tests:
  - ordering;
  - the caps;
  - a task with an active run isn't started twice;
  - the Schedule is created once and is idempotent across worker restarts (time-skipping environment).

## Out of scope

- GitHub Issues as a source (M2-08 onwards).
- Usage windows (M2-14); budgets (M6, v0.1).

## Acceptance criteria

- [ ] `uv run pytest` passes.
- [ ] The Temporal UI shows the `fact-scheduler` schedule, and pausing it there stops new runs.
- [ ] With the scheduler on, flipping a task to `ready` on `main` starts its run within a minute (checked by hand, noted in the PR).
