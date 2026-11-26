# fact-core

Shared foundation: settings, logging, database, migrations, project config, shared types and the activity contract, GitHub App access.

## Settings

`fact.core.settings.get_settings()` loads `fact.yaml` (layering in `AGENTS.md`). The per-step-kind limits (`StepDefaults`, `StepDefaultsSettings` and the agent and shell subclasses) live in `fact.core.step_defaults`; `fact.core.settings` re-exports `StepDefaults` and `StepDefaultsSettings`. Scheduler settings: `concurrency.max_runs` (default 2, runs queued, running or waiting across all projects), `concurrency.scheduler_enabled` (default false: the Schedule is created paused), `concurrency.scheduler_interval_seconds` (default 30) and `max_runs` on each `projects:` entry (default 1). `Settings.api_url` is the base URL clients use to reach the API (`http://<host>:<port>` from `api`; a wildcard bind host `0.0.0.0` or `::` becomes `127.0.0.1` or `::1`, IPv6 literals are bracketed). The worker and the `fact` CLI use it; don't rebuild the URL from `api.host`. `project_path(ref)` gives a project's local checkout from its `path` (relative to the folder of `fact.yaml`), or None.

## Database

Plain SQLAlchemy 2 async ORM on SQLite (aiosqlite). `fact.core.db` has `Base` (constraint naming convention), `database_url(settings)`, `create_engine(url)` (sets `foreign_keys=ON` and `journal_mode=WAL` on connect) and `session_factory(engine)`.

### Models

`fact.core.models` has `Project`, `Repository`, `Run`, `Step`, `Event`, `Notification` (a run's `gate_waiting`, `prs_ready`, `run_failed` or `run_finished` message for a person; `run_id` nullable, `read_at` set once read, `dedupe_key` unique so one notification exists per run state) and `UsageWindow` (table `usage_windows`: the last state an agent step reported of one subscription window, unique by `account` and `window`; `utilization` 0..1, `resets_at`, `updated_at`); `fact.core.enums` has the `StrEnum`s (`RunStatus`, `RunOutcome`, `StepKind`, `StepStatus`, `StepOutcome`, `NotificationKind`), stored as plain strings with no DB check. Only Postgres-compatible types are used (`String`, `Text`, `Integer`, `Float`, `Numeric(12, 6)` for USD, `DateTime(timezone=True)`, `JSON`). SQLite returns naive datetimes; treat them as UTC. New models must be imported in `migrations/env.py` (via `fact.core.models`) so autogenerate sees them.

### Migrations

Alembic lives in `core/migrations/`, configured by `core/alembic.ini`. The URL comes from settings (`<data_dir>/fact.db`) and the data dir is created if missing. Run from the repository root:

```
uv run alembic -c core/alembic.ini current
uv run alembic -c core/alembic.ini revision --autogenerate -m "message"
uv run alembic -c core/alembic.ini upgrade head
```

In code: `fact.core.migrate.upgrade(url)`. It is synchronous (the async `env.py` starts its own event loop), so don't call it from a running loop. Set `FACT_DATA_DIR` to point commands at another data dir.

## Project config

`fact.core.project_config` loads `<home>/.fact/project.yaml` (`load_project_config(home_path)` -> `ProjectConfig`) and `<repo>/.fact/repo.yaml` (`load_repo_config(repo_path)` -> `RepoConfig`; commands are argv lists, never strings). Invalid or missing files raise `ConfigError` with the file path and field. This repository's own `.fact/` is the first project.

## Task sources

Callers reach a project's tasks through `fact.core.tasks.source_for(project_config, settings, root=None, *, app=None)`, which returns a `TaskSource` (async `list_ready()` returning `ReadyItem`s with their dependency depth, `get(key)`, `deps_done(task)`, write-back `comment`, `set_labels`, `write_context`, `link_prs`, `close`, and sync `mark_done(worktree, key)`): a `FileTaskSource` on `root` for `files`, a `GitHubIssuesSource` for `github` (with `app`, else `fact.core.github.shared_app(settings)`, which raises `GitHubAppConfigError` while the App is not configured). `parse_key(key)` parses `task:M1-02` and `gh:owner/repo#12` into a `TaskKey`; `branch_name(key)` gives `fact/task-m1-02` or `fact/gh-12`; `dependency_key(task, dep)` turns a bare dependency id into a key of the task's own source. `fact.core.tasks.events.report(source, ReportStatusInput)` maps a run event to write-backs: `run_started` adds `fact:running` and comments the run link; `gate_opened` adds `fact:needs-human` and comments what the gate needs; `gate_answered` removes `fact:needs-human`; `prs_opened` links the PRs; `merged` closes the task and removes both labels; `closed` and `dropped` remove both labels and comment the end. Only `fact.core.tasks` and tests import `FileTaskSource`.

`fact.core.tasks.FileTaskSource(root)` reads `tasks/` (format in `tasks/README.md`) and implements `TaskSource`; its write-backs only log (`task_write_back_skipped`). Sync readers: `all()`, `find(key)`, `status(key)`, `all_deps_done(task)`, `ready()`, `validate()` (list of error strings) and `mark_done(worktree, key)` (deletes the task's file under `worktree` and returns its path) and `depths()` (each task's dependency depth by key: 0 with no dependency still in the folder, else one past its deepest dependency; a cycle is cut where it closes). Statuses are not on `Task`; use `status()`. A dependency whose file is missing counts as done if its number is below the highest remaining one in its milestone, or its milestone is below the highest present.

`fact.core.tasks.GitHubIssuesSource(repo, app)` (`github_issues.py`, M2-09; write-backs in `github_writes.py`, M2-11) reads one repository's issues (`gh:owner/repo#12`, branch `fact/gh-12`). `list_ready()` gives the open `fact:ready` issues (PRs excluded) whose dependencies are done, at depth 0. `get(key)` reads an issue of its own repository, open or closed (None for a PR, an unknown issue, another repository's or a file key): type from `type:*` (`type:incident` is `bug`, unknown is `feature`), complexity from `complexity:*` (default `standard`), acceptance criteria from the checklist under an "Acceptance criteria" heading of any level, `depends_on` from the blocked-by links (`issues/{n}/dependencies/blocked_by`, paginated) then `Depends on #3, owner/other#4` lines, deduplicated, as full `gh:` keys. A dependency is done when closed as completed or closed with no `state_reason`, or a merged PR; `not_planned` and `duplicate` are not done, and an unreadable one (404, App not installed) is not done and logs `task_issue_unreadable`. Issue payloads are cached per instance, so use one instance per request or tick. `mark_done` returns None.

Its write-backs (`GitHubIssueWrites`, a mixin) go to the repository the key names, with that repository's installation token. `comment` posts an issue comment; `set_labels` posts the labels to add and deletes each label to remove (a 404, label absent, is ignored); `link_prs` posts one comment listing the PRs, nothing when there are none; `close` sets the issue `closed` with `state_reason` `completed`; `write_context` reads the body afresh and patches it only when `with_context(body, section)` changed it. `with_context` puts the section between `<!-- fact:context:start -->` and `<!-- fact:context:end -->`: the last start marker followed by an end marker delimits it; with no such pair (no markers, a lone start, a lone end, an end before the start) a fresh pair is appended and stray markers stay; every byte outside the section is preserved. A key that is not `gh:` (a file task run on a project whose source is GitHub) is only logged (`task_write_back_skipped`), not an error.

## Shared types

`fact.core.types` (re-exported as `fact.sdk.types`) holds the frozen Pydantic models that cross the Temporal boundary: `Task`, `RepoCheckout`, `WorkspaceSpec`, `WorkspaceHandle`, `Usage`, `StepResult`, `PRRef`, `RunInput`, `RunResult`, plus `ArtifactRef` (a str key). They hold artifact refs, never raw logs, transcripts or diffs. Pass any text going into a payload through `limit_text(s, max_bytes)`, which cuts on a character boundary; `StepResult.summary` and `RunResult.summary` are capped at 2048 bytes (a validator rejects longer text; cut it with `limit_text`). `RunInput` carries `home_repo` (the repository holding `.fact/`; default the first) and `limits: RunLimits` (agent, shell and control activity timeouts in seconds), filled by whoever starts the run so workflow code reads no settings, and `handoff_ref` (artifact key of the handoff note a dropped run of the task left, or None). `RunOutcome` includes `pr_opened` (change set open for review), `needs_human` (the run stopped for a person, such as a rejected `needs_human` gate after an exhausted checks loop) and `dropped` (a person dropped the run with `fact drop`, or rejected its plan). `WorkspaceHandle.branches` maps each repository's full name to the branch actually checked out: the requested one, or `<branch>-<n>` when another workspace holds it.

## Activity contract

`fact.core.activity_contract` is the contract between workflow code (`fact.sdk`) and the worker: the task queues (`CONTROL_QUEUE`, `SANDBOX_QUEUE`), activity names (`prepare_workspace`, `teardown_workspace`, `agent_step`, `shell_step`, `open_change_set`, `mark_task_done`, `record_run_started`, `record_run_finished`, `record_gate_opened`, `record_gate_answered`, `notify`, `write_handoff`, among others), the `Check` literal and the frozen input models (`AgentStepInput`, with `handoff_ref`, `ShellStepInput`, `CheckResult`, `OpenChangeSetInput`, `MarkTaskDoneInput`, `RunStartedInput`, `RunFinishedInput`, `GateOpenedInput`, `GateAnsweredInput`, `NotifyInput`, `HandoffInput` and `HandoffStep` for a dropped run's handoff note, `ReportStatusInput` for `report_status`). Workflows call activities by these names, so `fact.sdk` never imports `fact.worker`.

## Gates, status and steering

`fact.core.gates` is the contract between a running workflow and whoever talks to it: the handler names `ANSWER_GATE` (`answer_gate`, an update taking a `GateAnswer`), `STATUS` (`status`, a query returning `WorkflowStatus`) and `STEER` (`steer`, a signal taking the text to add to the next agent session), so the API and CLI can call them by name without importing `fact.sdk`. Models: `GateDecision` (`approve`, `reject`), `GateAnswer` (`gate_id`, `decision`, `note` up to `NOTE_MAX_CHARS` 4000), `OpenGate` (`gate_id`, `kind`, `stage_id`, `attempt`, `details`, `opened_at`), `StageOutcome` and `WorkflowStatus` (`stage`, `attempt`, `gate`, `outcomes`); `STEER_MAX_CHARS` is 8000. They are re-exported from `fact.sdk.types`; the handlers are implemented by `fact.sdk.human.HumanSteps`.

## Scheduler

`fact.core.scheduler` is the scheduler's contract: `SCHEDULE_ID` (`fact-scheduler`, the Temporal Schedule), the workflow type `SchedulerTick` (control queue), the activity names `find_ready_tasks` and `start_scheduled_run`, and the models `ReadyTask` (`project`, `task_key`, `task_id`, `depth`), `ProjectLoad`, `ReadyTasks`, `StartScheduledRunInput` and `TickResult`. `plan_starts(ready)` is pure, so the tick workflow calls it: it orders tasks by dependency depth, then id (`M<n>-<n>` numerically, then issue numbers), and takes them while `concurrency.max_runs` and each project's `max_runs` allow, counting runs queued, running or waiting; a project missing from `projects` gets nothing. The worker owns the Schedule (`fact.worker.scheduler`); the API serves `find_ready_tasks` and pauses or unpauses the Schedule.

## GitHub

`fact.core.github` authenticates as the fact GitHub App. Configure it with `FACT_GITHUB_APP_ID` (the App ID or the Client ID; both work as the JWT issuer) and `FACT_GITHUB_APP_PRIVATE_KEY_PATH` (a PEM file outside any repository; a relative path resolves from the folder of `config_file()`, the default `fact.yaml` or `FACT_CONFIG`, like `.env`, even when `from_settings` gets a `Settings` object). `github_api_url` defaults to `https://api.github.com`.

- `GitHubApp.from_settings()` builds the App (raises `GitHubAppConfigError` if unset or the key is unreadable). Caches live on the instance: keep one `GitHubApp` per process. `shared_app(settings=None)` returns that one instance, built on first use and cached per App id, key path and API URL (an unconfigured App raises and is tried again on the next call); the worker and `source_for` use it.
- `app_jwt()` signs an RS256 App JWT (issued 60 s in the past, valid 9 minutes). `app_info()` and `installation(owner/repo)` read `/app` and the repository's installation.
- `installation_token(owner/repo)` returns an `InstallationToken(token, expires_at, installation)` that can reach only that repository. It is cached per repository (case-insensitive) until 5 minutes before expiry; concurrent calls for one repository mint once.
- `bot_identity()` gives the commit author: `<slug>[bot]` and `<user id>+<slug>[bot]@users.noreply.github.com` (unauthenticated user lookup; GitHub Enterprise Server is not supported).
- `github_client(token, *, api_url)` is a plain `httpx.AsyncClient` with bearer auth and the GitHub API headers (version `2022-11-28`), following redirects. Error responses raise `GitHubAuthError` (401), `GitHubForbiddenError` (403), `GitHubNotFoundError` (404) or `GitHubError`, with a hint, the method, path and GitHub's message.
- `GitHubApp.api_client(token)` returns a `github_client` on the App's API URL (and its test transport); the worker's `open_change_set` uses it.
- `sync_labels(client, owner/repo)` (`labels.py`) creates every label in `FACT_LABELS` (`fact:*`, `type:*`, `complexity:*`) on a repository and fixes colour, description and case of existing ones (names match case-insensitively). It never deletes a label and returns one `LabelChange` (`created`, `updated`, `unchanged`) per label. `split_full_name(owner/repo)` validates and splits a repository name.

Tokens never appear in logs or `repr()`; minting logs `github installation token minted` with the repository, installation id and expiry. Check a setup with `uv run fact github check <owner/repo>`; create fact's labels on a repository with `uv run fact github labels sync <owner/repo>`.

## Artifacts

`fact.core.artifacts` is the artifact store: the `ArtifactStore` protocol (`put_bytes`, `put_file`, `open`, `exists`), `LocalArtifactStore(root)` (rooted at `settings.artifacts_dir`) and `step_key(run_id, stage_id, attempt, name)` giving `runs/<run>/<stage>/<attempt>/<name>`, and `handoff_key(run_id)` giving `runs/<run>/handoff.md`, the note a dropped run leaves for the next run of its task (derived from the run id, so it needs no DB column). Keys with `..`, empty segments, absolute paths or drive letters raise `InvalidArtifactKeyError` (the API answers 400). The API serves them at `PUT`/`GET /api/v1/artifacts/{key}`.
