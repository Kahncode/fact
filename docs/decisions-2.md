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

### [2026-11-26 23:59] The task source interface is async, and file-specific readers keep sync names

**What:** `TaskSource` (`@core/src/fact/core/tasks/base.py`) is an async protocol: `list_ready()` (returning `ReadyItem` with a dependency depth, which the scheduler orders by), `get`, `deps_done`, and the write-backs; plus a sync `mark_done(worktree, key) -> str | None` so `mark_task_done` needs no `isinstance` (None: the source records completion elsewhere; `mark_task_done` still requires `Task.path` until M2-09 decides how an issue task skips it). `FileTaskSource` runs its reads in a thread; its former sync `get`/`deps_done` are now `find`/`all_deps_done`. `source_for(config, settings, root)` takes the checkout a `files` source reads (the API's snapshot of `origin/<default>`, the run's worktree, or the local checkout) and returns None for `github` until M2-09. `local_path` moved from `fact.api.projects` to `fact.core.settings.project_path` so the worker can find a project's checkout. The acceptance grep ("nothing outside `core/tasks/` imports `FileTaskSource`") holds for code; `FileTaskSource`'s own unit tests and the task-folder validation tests still import it.
**Why:** The GitHub source (M2-09) uses the async `GitHubApp`; a sync protocol would force it to bridge event loops. The spec's list has no depth, which the scheduler needs, so `list_ready` carries it rather than a second method.

### [2026-11-26 23:59] `report_status` maps events now; workflows start sending them in M2-11

**What:** The `report_status` control activity (`@worker/src/fact/worker/activities/task_status.py`, input `ReportStatusInput`) resolves the project's source and applies a `TaskEvent` via `fact.core.tasks.events.report`, with the label and comment mapping M2-11 lists. No workflow calls it yet.
**Why:** M2-08 must not change behaviour, and new activity calls in `FactWorkflow.execute` would change every workflow test's expected activity sequence; M2-11 owns sending the events (its task says so). Defining the mapping here lets M2-11 only implement the GitHub methods and add the calls.

### [2026-11-26 23:59] An issue dependency is done when closed as completed, closed without a reason, or a merged PR

**What:** `is_done` (`@core/src/fact/core/tasks/github_issues.py`) counts a dependency as done when its issue is `closed` with `state_reason` `completed` or with no `state_reason`, or when it is a PR with `merged_at` set. Closed as `not_planned` or `duplicate` is not done; neither is a dependency the App cannot read (404, or not installed on that repository), which logs `task_issue_unreadable`. M2-09's task text said only "closed as completed".
**Why:** Issues closed before GitHub recorded a close reason have `state_reason` null; treating them as not done would block every task depending on old work forever. A blocked-by link or a `Depends on #n` line may point at a PR, and a merged PR is the work landing. Not planned and duplicate mean the work did not happen, so the dependent task must wait for a person. An unreadable dependency fails safe: the task waits rather than starting on work fact cannot verify.

### [2026-11-26 23:59] `type:incident` runs as `bug`

**What:** `task_type` maps the label `type:incident` to `TaskType` `bug` (`TYPE_ALIASES`); an unknown or missing `type:*` label is `feature`.
**Why:** `TaskType` has no `incident`, and adding one would need a workflow for it in every project's `workflows` map. An incident's work today is a bug fix, so the bug workflow is the closest fit. The follow-up (add the type when incidents need their own workflow) is in `tasks/backlog.md`.

### [2026-11-26 23:59] One `GitHubApp` per process through `shared_app`

**What:** `fact.core.github.shared_app(settings)` returns one `GitHubApp` per App configuration (App id, key path, API URL), built on first use; an unconfigured App raises `GitHubAppConfigError` and is retried on the next call. `source_for` uses it for `github` sources unless given `app`, and the worker's `_github_app` uses it instead of building its own.
**Why:** Installation tokens are cached on the `GitHubApp` instance. The API opens a task source per request and per scheduler tick; building an App each time would mint a new token per read and hit GitHub's rate limits. Passing an App through every caller of `source_for` (API routes, scheduler, worker activities) would thread it through code that otherwise knows nothing of GitHub. Keying by configuration gives a different App configuration its own instance instead of reusing a stale one.

### [2026-11-26 23:59] The GitHub Issues source only logs its write-backs until M2-11

**What:** `GitHubIssuesSource.comment`, `set_labels`, `write_context`, `link_prs` and `close` log `task_write_back_skipped` with `source=github`; `mark_done` returns None, and `mark_task_done` returns False for a `gh:` task without touching the worktree.
**Why:** M2-09 is reading only; M2-11 owns writing to issues and sending the run events. Logging keeps `report_status` and the workflow path identical for both sources, so M2-11 only fills in the methods. An issue is closed when its change set merges, not by a commit on the branch, so there is nothing for `mark_done` to change.

### [2026-11-26 23:59] GitHub read failures surface as `TaskReadError`

**What:** `project_tasks` (`@api/src/fact/api/task_ref.py`) converts `GitHubError` and `httpx.HTTPError` raised while opening or reading a `github` source, including `GitHubAppConfigError` for an unconfigured App, into `TaskReadError`. `POST /api/v1/runs/start` answers 502 for it when the project is given (otherwise the project is skipped and named in the 404); the scheduler skips the project with `scheduler_skipped_project`.
**Why:** The API and scheduler already treat `TaskReadError` (a failed git read of task files) as "this project's tasks cannot be read now"; GitHub being down, refusing, or not set up is the same condition. One error type keeps the callers source-agnostic, and 502 says an upstream failed rather than the request being wrong. A missing App on one project must not stop the scheduler starting other projects' tasks.

### [2026-11-26 23:59] The issue's context section is the last start marker and the end marker after it; anything else appends a fresh pair

**What:** `with_context` (`@core/src/fact/core/tasks/github_writes.py`) replaces the text between the last `<!-- fact:context:start -->` and the first `<!-- fact:context:end -->` after it. With no such pair (no markers, a lone start, a lone end, an end before the start) it appends a new pair with the section and leaves the stray markers where they are. Every byte outside the section is kept; marker text inside the section is stripped. `write_context` re-reads the body right before writing and PATCHes only when it changed.
**Why:** The issue body belongs to a person, who may edit or paste over the markers. Taking the last start keeps a quoted or older start marker higher up from swallowing their text; refusing to guess on a broken pair means text written after a lone start is never overwritten, at the cost of a duplicate section a person can tidy. A fresh read narrows the window for losing a concurrent edit, and skipping an unchanged PATCH avoids a no-op edit in the issue history.

### [2026-11-26 23:59] A write-back for a key of another source is logged, not an error

**What:** `GitHubIssueWrites` writes to the repository the `gh:` key names (with that repository's installation token), not to the source's `repo`. A key that is not `gh:` (a `task:` file task run on a project whose source is GitHub) logs `task_write_back_skipped` and returns.
**Why:** `fact run` can start a file task on any project, and the file source already treats its write-backs as logs; raising would turn bookkeeping into run failures (through `report_status` retries) for a run that has no issue to update. Using the key's repository keeps the write correct if a run's task lives in a repository other than the source's.

### [2026-11-26 23:59] Task status reporting is best effort, and a GitHub 4xx other than 429 is not retried

**What:** `FactWorkflow.report_task` (`@sdk/src/fact/sdk/task_status.py`) runs `report_status` and, on an `ActivityError`, calls `stop_if_dropped()` and then only logs a warning. The `report_status` activity turns a `GitHubError` with a 4xx status other than 429 into a non-retryable `ApplicationError`; 429, 5xx and network errors retry under `INFRA_RETRY`.
**Why:** Labels and comments on the tracker are bookkeeping; a tracker outage or a missing Issues permission must not fail or stall a run whose code work is fine. A 401/403/404/422 fails the same way on every attempt, so retrying it only delays the run by the whole retry budget at each event. `stop_if_dropped()` matters because a drop cancels the running report activity: swallowing that error as a reporting failure would let the run carry on after a person dropped it. Known gap: a 403 secondary rate limit is treated as permanent (see `tasks/backlog.md`).

### [2026-11-26 23:59] Which run events reach the task, and from where

**What:** `FactWorkflow.execute` sends `run_started` after recording the start, and at the end `report_run_end` (after `record_run_finished`, before teardown) sends `merged`, `closed` or `dropped` (a rejected plan included). `HumanSteps.gate` sends `gate_opened` (detail: the gate's summary and the `fact gate <run> approve|reject` hint) and `gate_answered`; `PullRequestSteps.open_change_set` sends `prs_opened` once, not on later CI-fix pushes; `DropSteps.drop` sends `dropped` with the cancellation reason after teardown, before recording the outcome. The `wait_for_review` wait, recorded as a gate-like step, sends no gate events. `failed`, `needs_human` and `pr_opened` ends send nothing yet, so `fact:running` stays on the issue.
**Why:** Sending from the base class and its mixins gives every project workflow the reporting without code of its own, as notifications and drops already do. Reporting the end after the run row is finished keeps the DB the first record; `drop()` reports before recording `dropped` so a run shown `dropped` has already told its task, as it already has its note and free branch. Review waiting is not a gate a person answers with `fact gate`, so a `fact:needs-human` label there would be wrong. The failure ends need their own design (swap `fact:running` for `fact:needs-human` and say why); they are left as a follow-up in `tasks/backlog.md` rather than reported half-way.
