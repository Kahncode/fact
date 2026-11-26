# Decisions (continued)

Continuation of `docs/decisions.md`, which reached the 300-line limit; earlier entries are there. Design-level decisions are in `docs/spec/16-decisions.md`. Entries are append-only.

## Decisions

### [2026-11-26 23:59] The scheduler never re-offers a task that has had a run, whatever its outcome

**What:** `find_ready` (`@api/src/fact/api/scheduler.py`) drops every ready task that has any run row counted by `counts_as_run()` (`@api/src/fact/api/run_start.py`): active or ended, any outcome. A start that never reached Temporal (`finished`, `started_at` and `ended_at` null, as `_mark_start_failed` leaves it) does not count. The scheduler's starts send `only_if_never_run: true` to `POST /api/v1/runs/start`, which answers 409 when the task already has a run. A person restarts a task whose run failed, needed a person, was dropped or had its PR closed with `fact run`. M2-07's task text asked to skip only tasks with an active or `merged` run.
**Why:** With only active and merged runs excluded, a task whose run ended `failed`, `needs_human`, `dropped` or `closed` stays `ready` on the default branch and would be restarted on every tick (30 s), spending agent time on the same failure. Leaving the restart to a person matches how drops and gates already work. A start that failed before Temporal has no workflow and did no work, so it must not block the task. `only_if_never_run` re-checks inside the start request, narrowing the window where a manual `fact run` lands between a tick's read and its start.

### [2026-11-26 23:59] Per-project `max_runs` defaults to 1

**What:** `ProjectRef.max_runs` (`@core/src/fact/core/settings.py`), the scheduler's cap per project counting runs `queued`, `running` or `waiting`, defaults to 1; M2-07's task text said 2. `concurrency.max_runs` (all projects) defaults to 2.
**Why:** The entry "Workspaces run each repository's setup commands, which install the git hooks" (2026-11-26 10:30, in `docs/decisions.md`) leaves a known limit: a bare mirror's hooks are shared by every worktree of that repository, and `pre-commit install` pins the newest workspace's `.venv` Python, so two runs of one repository at once can break each other's commits once one workspace is torn down. That entry says the scheduler must account for it; one run per project at a time does. Raise the default once hooks are installed per worktree.

### [2026-11-26 23:59] The worker owns the Schedule; `scheduler_enabled` applies only when it is created, and the tick goes through the API

**What:** `ensure_schedule` (`@worker/src/fact/worker/scheduler.py`) runs at every worker start. It creates the `fact-scheduler` Schedule when missing (overlap `SKIP`, every `concurrency.scheduler_interval_seconds`, paused unless `concurrency.scheduler_enabled`); on an existing Schedule it updates only the interval, and never pauses or unpauses it. After creation, `fact scheduler on|off` (`POST /api/v1/scheduler/on|off`) or the Temporal UI decide whether it runs. Changes to the Schedule's action or policy reach an existing Schedule only once it is deleted and recreated. The `SchedulerTick` workflow (control queue) reads candidates through `find_ready_tasks` (`GET /api/v1/scheduler/ready`) and starts each through `start_scheduled_run` (`POST /api/v1/runs/start`); a start is never retried (`maximum_attempts=1`), and a failed or refused start does not stop the tick.
**Why:** If every worker restart re-applied `scheduler_enabled`, a pause made with `fact scheduler off` or in the Temporal UI would be undone silently by the next `fact dev`; the setting only picks the initial state, so a fresh install starts paused and safe. The API already owns git reads of `origin/<default>` and the DB, so the activities call it instead of duplicating that in the worker, and scheduled starts take the same code path, checks and workflow-id reservation as `fact run`. A start whose answer was lost may still have started the run, so retrying it could start a second one; the next tick picks the task up again if it really did not start. Overlap `SKIP` keeps a slow tick (a `git fetch` per project) from stacking.

### [2026-11-26 23:59] The Schedule tests use the Temporal dev server, not the time-skipping one

**What:** `test_schedule_created_once_and_follows_the_interval` (`@worker/tests/test_scheduler.py`) and `test_scheduler_on_off_status` (`@api/tests/test_scheduler.py`) run against `WorkflowEnvironment.start_local()`, the Temporal dev server. The `SchedulerTick` tests, which start the workflow directly with no Schedule, stay on the time-skipping environment. M2-07's task text asked for the time-skipping environment for the Schedule test.
**Why:** Temporal's time-skipping test server does not implement `CreateSchedule`, so a Schedule cannot be created there. `start_local()` runs the real dev server, which does.
