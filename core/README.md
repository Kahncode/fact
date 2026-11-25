# fact-core

Shared foundation: settings, logging, database, migrations, project config, shared types and the activity contract, GitHub App access.

## Settings

`fact.core.settings.get_settings()` loads `fact.yaml` (layering in `AGENTS.md`). `Settings.api_url` is the base URL clients use to reach the API (`http://<host>:<port>` from `api`; a wildcard bind host `0.0.0.0` or `::` becomes `127.0.0.1` or `::1`, IPv6 literals are bracketed). The worker and the `fact` CLI use it; don't rebuild the URL from `api.host`.

## Database

Plain SQLAlchemy 2 async ORM on SQLite (aiosqlite). `fact.core.db` has `Base` (constraint naming convention), `database_url(settings)`, `create_engine(url)` (sets `foreign_keys=ON` and `journal_mode=WAL` on connect) and `session_factory(engine)`.

### Models

`fact.core.models` has `Project`, `Repository`, `Run`, `Step` and `Event`; `fact.core.enums` has the `StrEnum`s (`RunStatus`, `RunOutcome`, `StepKind`, `StepStatus`, `StepOutcome`), stored as plain strings with no DB check. Only Postgres-compatible types are used (`String`, `Text`, `Integer`, `Float`, `Numeric(12, 6)` for USD, `DateTime(timezone=True)`, `JSON`). SQLite returns naive datetimes; treat them as UTC. New models must be imported in `migrations/env.py` (via `fact.core.models`) so autogenerate sees them.

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

## Task files

`fact.core.tasks.FileTaskSource(root)` reads `tasks/` (format in `tasks/README.md`): `all()`, `get(key)`, `status(key)`, `deps_done(task)`, `ready()`, `validate()` (list of error strings) and `mark_done(worktree, key)` (rewrites only the status value in the front matter). Statuses are not on `Task`; use `status()`. A dependency whose file is missing counts as done if its number is below the highest remaining one in its milestone, or its milestone is below the highest present. `branch_name(key)` returns the task branch name.

## Shared types

`fact.core.types` (re-exported as `fact.sdk.types`) holds the frozen Pydantic models that cross the Temporal boundary: `Task`, `RepoCheckout`, `WorkspaceSpec`, `WorkspaceHandle`, `Usage`, `StepResult`, `PRRef`, `RunInput`, `RunResult`, plus `ArtifactRef` (a str key). They hold artifact refs, never raw logs, transcripts or diffs. Pass any text going into a payload through `limit_text(s, max_bytes)`, which cuts on a character boundary; `StepResult.summary` and `RunResult.summary` are capped at 2048 bytes (a validator rejects longer text; cut it with `limit_text`). `RunInput` carries `home_repo` (the repository holding `.fact/`; default the first) and `limits: RunLimits` (agent, shell and control activity timeouts in seconds), filled by whoever starts the run so workflow code reads no settings. `RunOutcome` includes `pr_opened` (change set open for review) and `needs_human` (the run gave up, such as an exhausted checks loop).

## Activity contract

`fact.core.activity_contract` is the contract between workflow code (`fact.sdk`) and the worker: the task queues (`CONTROL_QUEUE`, `SANDBOX_QUEUE`), activity names (`prepare_workspace`, `agent_step`, `shell_step`, `open_change_set`, `mark_task_done`, `record_run_started`, `record_run_finished`), the `Check` literal and the frozen input models (`AgentStepInput`, `ShellStepInput`, `CheckResult`, `OpenChangeSetInput`, `MarkTaskDoneInput`, `RunStartedInput`, `RunFinishedInput`). Workflows call activities by these names, so `fact.sdk` never imports `fact.worker`.

## GitHub

`fact.core.github` authenticates as the fact GitHub App. Configure it with `FACT_GITHUB_APP_ID` (the App ID or the Client ID; both work as the JWT issuer) and `FACT_GITHUB_APP_PRIVATE_KEY_PATH` (a PEM file outside any repository; a relative path resolves from the folder of `config_file()`, the default `fact.yaml` or `FACT_CONFIG`, like `.env`, even when `from_settings` gets a `Settings` object). `github_api_url` defaults to `https://api.github.com`.

- `GitHubApp.from_settings()` builds the App (raises `GitHubAppConfigError` if unset or the key is unreadable). Caches live on the instance: keep one `GitHubApp` per process.
- `app_jwt()` signs an RS256 App JWT (issued 60 s in the past, valid 9 minutes). `app_info()` and `installation(owner/repo)` read `/app` and the repository's installation.
- `installation_token(owner/repo)` returns an `InstallationToken(token, expires_at, installation)` that can reach only that repository. It is cached per repository (case-insensitive) until 5 minutes before expiry; concurrent calls for one repository mint once.
- `bot_identity()` gives the commit author: `<slug>[bot]` and `<user id>+<slug>[bot]@users.noreply.github.com` (unauthenticated user lookup; GitHub Enterprise Server is not supported).
- `github_client(token, *, api_url)` is a plain `httpx.AsyncClient` with bearer auth and the GitHub API headers (version `2022-11-28`), following redirects. Error responses raise `GitHubAuthError` (401), `GitHubForbiddenError` (403), `GitHubNotFoundError` (404) or `GitHubError`, with a hint, the method, path and GitHub's message.
- `GitHubApp.api_client(token)` returns a `github_client` on the App's API URL (and its test transport); the worker's `open_change_set` uses it.
- `sync_labels(client, owner/repo)` (`labels.py`) creates every label in `FACT_LABELS` (`fact:*`, `type:*`, `complexity:*`) on a repository and fixes colour, description and case of existing ones (names match case-insensitively). It never deletes a label and returns one `LabelChange` (`created`, `updated`, `unchanged`) per label. `split_full_name(owner/repo)` validates and splits a repository name.

Tokens never appear in logs or `repr()`; minting logs `github installation token minted` with the repository, installation id and expiry. Check a setup with `uv run fact github check <owner/repo>`; create fact's labels on a repository with `uv run fact github labels sync <owner/repo>`.

## Artifacts

`fact.core.artifacts` is the artifact store: the `ArtifactStore` protocol (`put_bytes`, `put_file`, `open`, `exists`), `LocalArtifactStore(root)` (rooted at `settings.artifacts_dir`) and `step_key(run_id, stage_id, attempt, name)` giving `runs/<run>/<stage>/<attempt>/<name>`. Keys with `..`, empty segments, absolute paths or drive letters raise `InvalidArtifactKeyError` (the API answers 400). The API serves them at `PUT`/`GET /api/v1/artifacts/{key}`.
