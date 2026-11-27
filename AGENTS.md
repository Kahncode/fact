# fact

---

## Project overview

fact is a software factory: a service that turns tracker issues into reviewed GitHub PRs using coding agents, with Temporal running every workflow durably.

- Works on any git + GitHub project, including projects that span several repositories.
- Takes work from a task tracker (GitHub Issues first, Linear next) and has a planning loop that turns ideas into specs and issues.
- Workflows are code combining agents, deterministic tools and persistent data; they can change often without breaking runs in flight.
- Every step is inspectable: logs, agent transcript, wall time, human wait time, tokens and cost.
- Humans review in GitHub PRs; the UI follows progress and notifies you when something needs you.

- **Design:** `docs/spec/`, one file per section. Start from `docs/spec/README.md` and read only what your task points to.
- **Architecture:** `docs/ARCHITECTURE.md`, index of components and what exists in code today.
- **Decisions:** `docs/decisions.md`, implementation decisions, continued in `docs/decisions-2.md` (new entries go there; design decisions are in `docs/spec/16-decisions.md`).
- **Work:** GitHub issues on `Kahncode/fact` (`fact:ready` issues are tasks the scheduler may start; labels in `docs/spec/12-integrations.md`). `tasks/backlog.md` holds ideas and problems not yet planned. The file tasks under `tasks/` are the M0–M2 bootstrap; `tasks/README.md` explains their format.

---

## Project structure

```
fact/
├── AGENTS.md, CLAUDE.md   instructions (CLAUDE.md is the @AGENTS.md shim)
├── fact.yaml              global settings and project registry
├── pyproject.toml         uv workspace root, ruff/black/import-linter/pytest config
├── core/                  fact.core: settings, logging, DB models, migrations, artifact store, shared types
├── api/                   fact.api: FastAPI app
├── worker/                fact.worker: Temporal workers and activities
├── sdk/                   fact.sdk: step library and workflow base class that project workflows import
├── cli/                   fact.cli: the `fact` Typer command
│   (each package: src/fact/<name>/ and tests/)
├── scripts/               development scripts (openapi_snapshot.py); scripts/tests/ tests the .claude/hooks scripts
├── web/                   React UI (from M4)
├── .fact/                 fact's own project config, workflows and prompts
├── docs/spec/             design, one file per section (start at README.md)
├── tasks/                 backlog.md, and the file tasks left from the M0–M2 bootstrap
├── .github/workflows/     CI (ci.yaml)
├── .agents/skills/        shared agent skills (canonical)
├── .agents/agents/        subagent definitions (canonical)
└── .claude/               hooks, settings; skills/ and agents/ are gitignored projections of .agents/
```

Some folders appear only as the M0 tasks create them.

Skills and subagents are edited only under `.agents/`. There is no cross-tool standard for subagent files, so `.agents/agents/` is our own convention: the canonical copy, in Claude Code's markdown-with-frontmatter format (the only harness we run today). The SessionStart hook `.claude/hooks/project_dot_agents.py` projects `.agents/skills` and `.agents/agents` into `.claude/skills` and `.claude/agents` (a symlink or junction, or a managed copy where linking is not possible); never commit or edit files under those two projections.

---

## Tech stack details

| Decision       | Choice                                         | Why                                                                        |
| -------------- | ---------------------------------------------- | -------------------------------------------------------------------------- |
| Language / env | Python 3.15, uv workspace                      | One lockfile, namespace packages `fact.*`                                  |
| Orchestration  | Temporal                                       | Durable workflows; deterministic workflow code, side effects in activities |
| API            | FastAPI, Pydantic v2                           | Plain routers, typed models                                                |
| Database       | SQLAlchemy 2 async ORM                         | Plain ORM, no extra abstraction layers                                     |
| CLI            | Typer                                          | `fact` command                                                             |
| Settings       | pydantic-settings + `fact.yaml`                | Layered config, `FACT_*` env overrides                                     |
| Lint / format  | ruff (`select = ALL`), black (line length 120) | Enforced by pre-commit                                                     |
| Boundaries     | import-linter (`[tool.importlinter]`)          | Package dependencies checked by pre-commit and CI                          |
| Tests          | pytest, pytest-asyncio (auto mode), pytest-timeout (60 s) | Per-package `tests/`; a deadlocked test fails with its stack       |

See `docs/spec/13-storage-and-stack.md` for the full stack rationale.

---

## Development commands

| Task                    | Command                                         |
| ----------------------- | ----------------------------------------------- |
| Install                 | `uv sync`                                       |
| Run all tests           | `uv run pytest`                                 |
| Run one package's tests | `uv run pytest core/tests`                      |
| Run a single test       | `uv run pytest path/to/test_file.py::test_name` |
| Run the API             | `uv run python -m fact.api`                     |
| Run Temporal, API, worker | `uv run fact dev`                             |
| Pin the factory version | `uv run fact use <ref>`                         |
| Run the pinned factory  | `uv run fact dev --pinned`                      |
| List registered projects | `uv run fact projects list`                    |
| Start a run for a task  | `uv run fact run task:<id>` or `gh:<owner/repo>#<n>` (`--force-deps`, `--project`) |
| List runs               | `uv run fact runs` (`--project`, `--status`)    |
| Show a run, its steps and why it waits | `uv run fact status <run id or workflow id>` |
| Answer a run's open gate | `uv run fact gate <run> approve\|reject` (`--note`, `--gate`) |
| Send instructions to a run | `uv run fact steer <run> TEXT`               |
| Stop a run (then `fact run` can restart its task; a task has one active run at a time) | `uv run fact stop <run>` (`--reason`) |
| Turn the scheduler on/off (dev only), show it and preview its next tick | `uv run fact scheduler on\|off\|status` |
| Run every ready task    | `uv run fact dev` (the scheduler is on at every API start in dev) |
| List unread notifications | `uv run fact inbox` (`--all` includes read)   |
| Mark a notification read | `uv run fact inbox read <id>`                  |
| Check GitHub App access | `uv run fact github check <owner/repo>`         |
| Create fact's labels on a repo | `uv run fact github labels sync <owner/repo>` |
| Regenerate the OpenAPI snapshot `api/openapi.json` (after an intended API change; `api/tests/test_openapi_compat.py` fails until you do) | `uv run python scripts/openapi_snapshot.py` |
| Lint (autofix)          | `uv run ruff check . --fix`                     |
| Format                  | `uv run black .`                                |
| Check package boundaries | `uv run lint-imports`                          |
| Install git hooks       | `uv run pre-commit install --install-hooks`     |

**Prerequisites:** Python 3.15 and uv; the Temporal CLI (`temporal`) on PATH for `fact dev`. Copy `.env.example` to `.env` for local secrets.

---

## Key patterns and conventions

- Packages share the `fact.*` namespace; dependencies point inward: `api`, `worker`, `sdk`, `cli` depend on `core`, and `worker` also on `sdk`.
- Package boundaries are import-linter contracts in `[tool.importlinter]` of `pyproject.toml` (the dependency direction above, clients never import `fact.sdk`, only `fact.core.tasks` imports `FileTaskSource`), checked by `uv run lint-imports` in pre-commit and CI; `sdk/tests/test_workflow_imports.py` checks that `.fact/workflows/` imports only `fact.sdk` and `fact.core` from fact. Fix a violation rather than loosen a contract; an exception goes in `ignore_imports` with a follow-up issue.
- Temporal: workflows are deterministic and orchestrate only; every side effect is an activity.
- Large data (logs, transcripts, diffs) goes to the artifact store; Temporal payloads carry references.
- Project workflows import the step library from `fact.sdk` and subclass its workflow base class (`fact.sdk.workflow.FactWorkflow`).
- When editing a workflow, do no unnecessary work and add no artificial waiting: react to events rather than polling (webhooks once hosted), keep poll intervals short, and read state from its single source of truth (a PR's state from GitHub) instead of copying or predicting it. Keep the graph simple: states that loop over the same steps, not special-case paths. See "Workflow design principles" in `docs/spec/06-workflow-authoring.md`.
- Queue names, activity names and activity inputs live in `fact.core.activity_contract`; `fact.sdk` calls activities by name and never imports `fact.worker`.
- Workflow handler names for people (`answer_gate` update, `status` query, `steer` signal) and their models live in `fact.core.gates`; clients call them by name and never import `fact.sdk`. A subclass of `FactWorkflow` that defines `__init__` must call `super().__init__()`.
- Agent sessions run through `FactWorkflow.agent`, defined in the `fact.sdk.agents.AgentSteps` mixin (resume rules in `fact.sdk.sessions`). A step the Claude usage limit refused ends `rate_limited`; `agent` marks the run `waiting` (`record_run_waiting` control activity, `WorkflowStatus.waiting`), sleeps on a durable timer until the step's `resume_at` and resumes the same session, up to `MAX_LIMIT_PAUSES` (20) times, counted apart from `MAX_RESUMES`, then the step ends `needs_human`. Project workflows never see `rate_limited`.
- A published API never changes: `api/openapi.json` is the committed OpenAPI snapshot and `api/tests/test_openapi_compat.py` (`fact.api.openapi_compat`) fails on any removed or changed path, method, parameter, field or type, and on any addition until the snapshot is regenerated. Break compatibility only when the task asks for it.
- Every agent step reports the usage windows it saw to `PUT /api/v1/usage/claude` (table `usage_windows`); `GET /api/v1/usage` lists them.
- Stopping a run (`fact stop`) cancels its workflow; `FactWorkflow.execute` turns the cancellation into outcome `stopped` (`fact.sdk.stop.StopSteps`), so project workflows need no stop handling of their own.
- The cost comment is posted once, by `FactWorkflow.execute`, as the last step of every run ending with a PR (stopped and failed included); project workflows don't post it themselves.
- Usage data is only ever what a harness reported: tokens and cost come from Claude Code's messages, never estimated or defaulted. An unknown cost is NULL (`Usage.cost_usd = None`, `steps.cost_usd` NULL), never 0; totals sum known costs and count the missing ones (`runs.cost_unknown_steps`).
- `python -m fact.worker` loads each project's `.fact/workflows/` from a detached checkout of its default branch at `pinned_dir/projects/<project>/<sha>` and runs them on queue and deployment `wf-<project>` (`workflow_queue`), pinned to build id `<factory SHA[:12]>+<home SHA>`; workflow changes apply at the next worker restart (see `worker/README.md`).
- The scheduler is the only background job, and it runs in the API, not in Temporal (Temporal runs only project workflows): `run_tick()` (`fact.api.scheduler`) reads the ready tasks (file tasks or `fact:ready` issues), plans them and starts each through `start_task`, the code of `POST /runs/start`, with `scheduled: true`, in `plan` order within `concurrency.max_runs` and each project's `max_runs` (`fact.core.scheduler`, `fact.api.scheduler`). `plan` orders each project's tasks by effective priority (`Task.priority` from `priority:*` labels, the highest when several, or a file's `priority:`; default `medium`; raised to that of every ready task of the project waiting on it, `effective_priorities`), then depth, then id; projects take turns in name order for the shared `concurrency.max_runs`. A tick that finds another running in the process (`asyncio.Lock`) returns at once (`skipped`). `scheduler_refusal` refuses (409) a scheduled start whose task already has a run, is failure-capped, or whose factory or project cap is reached; the tick logs a refused start and skips it.
  - Triggers: `POST /api/v1/scheduler/tick` runs one tick in every environment; outside dev it is the only one (an outside cron or a webhook). In dev only (`settings.is_dev_environment()`: `local`, `test`, `dev`), the lifespan starts a loop (`fact.api.scheduler_loop`) that ticks every `concurrency.scheduler_interval_seconds` (30) while the scheduler is on: in-process state, on at every API start, flipped by `fact scheduler on|off` (`POST /scheduler/on|off`, mounted only in dev). The worker deletes a leftover `fact-scheduler` Temporal Schedule at start (`fact.worker.scheduler`).
  - Run history (`run_history_hold`): a task with a run in progress, or whose last run ended any way but `failed`, is not offered (a person restarts it with `fact run`). A task whose runs failed is retried until `concurrency.max_failed_runs` (3) failed in a row, counted back to the latest manual run (`Run.trigger` `manual`|`scheduler`), then held `failure_cap`.
  - Usage gate (`claude.usage_gate`, off when `ANTHROPIC_API_KEY` is set in the API's environment): only when a tick has something to start, it asks `fact.api.usage_gate.UsageGate` (`app.state.scheduler.gate`), which keeps the last answer in the API process and reads usage with the local Claude Code login (the API runs on the same machine). A window at or above `1 - claude.reserve.*` (`five_hour` 0.10, `weekly` 0.05) starts nothing, with no further call until its reset plus 90 s; unreadable usage logs an error and runs start as usual. Runs already going are never touched.
  - `fact scheduler status` previews the next tick (`GET /api/v1/scheduler/preview`, the same `plan`): start order, each task's priority (and effective priority when higher), its `Hold` (`parent`, `dependency`, `failure_cap`, `cap`, `usage`) and the gate's last answer (`UsageGate.last`), with whether the scheduler is on and its last tick (`GET /api/v1/scheduler`). The preview never calls the usage endpoint.
  - Keep-awake (`worker.keep_awake`, Windows only): the worker holds `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` while `GET /scheduler/active` counts a run, polled every 60 s on the event-loop thread (`fact.worker.keep_awake`); never call it through `asyncio.to_thread` or an executor.
- Tasks are read and reported through the async `TaskSource` protocol from `fact.core.tasks.source_for(project_config, settings, root)`: `FileTaskSource` for `files`, `GitHubIssuesSource` for `github` (open `fact:ready` issues whose every ancestor is `fact:ready`, with open sub-issues as dependencies; the tick closes finished `type:epic` issues; write-backs comment, label, edit the context section and close the issue). Keys carry their source (`task:M1-02`, `gh:owner/repo#12`; parse with `parse_key`, branch from `branch_name`). Only `fact.core.tasks` (and tests) import `FileTaskSource`. A project's local checkout comes from `fact.core.settings.project_path(ref)`.
- Workflows tell the task's source what a run does only through `FactWorkflow.report_task(event, ...)` (`fact.sdk.task_status`), which runs the `report_status` control activity best effort; `fact.core.tasks.events.report` maps each `TaskEvent` to labels, comments and closing. Never call a `TaskSource` write-back from elsewhere in a run.
- Each package owns its tests in its own `tests/`. Write, change and review tests by the `testing` skill (`.agents/skills/testing/SKILL.md`): only tests that earn their place, deterministic and fast.
- Logging goes through `fact.core.logging` (structlog): call `configure_logging(env, level)` once at process start, get loggers with `get_logger(name)`, and wrap run/step work in `bind_run(run_id, step_id)` so every line carries `run_id`.
- Settings come only from `fact.core.settings.get_settings()`, which loads `fact.yaml`:
  - Priority, low to high: defaults, `fact.yaml`, `fact.<env>.yaml`, `.env`, `FACT_*` env vars with `__` nesting.
  - `FACT_CONFIG` points at another `fact.yaml`; `.env` is read from the same folder. Tests ignore it: the root `conftest.py` unsets it, so they always read this checkout's `fact.yaml`.
  - `${VAR}` in YAML expands from the environment or a file path.
  - Settings are frozen; an app may subclass `Settings` via `override_settings_class`.
  - Clients (worker, CLI) reach the API at `settings.api_url`; don't build the URL from `api.host`/`api.port` yourself.
- GitHub access goes through `fact.core.github`: one `GitHubApp` per process from `shared_app(settings)` (its token caches are per instance), tokens from `installation_token(owner/repo)`, HTTP via `github_client`. Never log or print a token.

---

## Dogfooding

fact works on its own tasks, so the running factory is never the code an agent edits: it runs from a pinned checkout, and agents only work in worktrees under `workspaces_dir` (`<data_dir>/workspaces`). `LocalWorktreeRunner.prepare` refuses a workspace inside `pinned_dir` or inside the factory checkout that is running.

- **Pin a version:** `uv run fact use main` fetches `origin`, checks `origin/main` (or any branch, tag or SHA) out as a detached worktree at `<data_dir>/pinned/factory/<sha>`, runs `uv sync --locked` there and writes the SHA to `<data_dir>/pinned/current`.
- **Run fact on its own tasks:** `uv run fact dev --pinned` starts Temporal here and the API and worker from the pinned version (the checkout's own `.venv` Python, without the venv activation `fact dev` was started with), still reading this tree's `fact.yaml` and `.env` through `FACT_CONFIG`. Then `uv run fact run gh:Kahncode/fact#<n>`, or let the scheduler start ready tasks (on at every API start; `uv run fact scheduler off` stops it). Until auto-merge follows the classification, `Standard` classifies and labels its PR (`fact:auto-merge` or `fact:needs-review`) and then merges it once CI is green whatever the label (`MERGE_WHEN_GREEN` in `.fact/workflows/standard.py`) and the run ends `merged`; `main` is merged into the branch only when GitHub says the PR cannot merge (it conflicts, read while waiting for CI, or the merge is refused as out of date): `sync_base` makes a merge commit, conflicts go to a `resolve_conflicts` agent session, then it pushes, waits for CI and merges again, at most twice; only when GitHub refuses the merge otherwise does the run stay open (`waiting`, stage `wait_for_review`) until you merge or close the PR. After merging fact changes, run `fact use main` again and restart.
- **Restart only once runs have ended:** a run is pinned to the worker build that started it, and one worker process runs only the newest build, so a run still waiting for review when the worker restarts on new code stalls and never sees its PR merge. After merging a run's PR, wait until it shows `merged` (within about 60 s) before `fact use main` and the restart. Stop runs still in progress or in review with `fact stop <run>` and wait until they show `stopped` before restarting: the stop is processed by the run's own worker version. A run whose version no worker runs anymore (or started before M2-06) never processes a stop; terminate it in Temporal and delete its `<data_dir>/workspaces/<run_id>` by hand, else the next run of the task gets a suffixed branch and a new PR.
- **Restart a task on newer code:** `uv run fact stop <run> --reason "..."`; the run ends `stopped`, writes a handoff note (`runs/<run_id>/handoff.md` in the artifact store), tears its workspace down and keeps its remote branch and PR. Then `fact use main`, restart, and `uv run fact run gh:Kahncode/fact#<n>`: the new run resumes the task branch from `origin`, reuses the open PR and gets the note as `$handoff` in its prompts. Local commits the stopped run never pushed are lost.
- **If a run goes wrong:** stop it as above, or stop `fact dev` (Ctrl-C), fix or finish the task by hand with Claude Code in this source tree, and pin the new version with `fact use main` once the fix is merged. A run that does not end `pr_opened`, `merged`, `closed` or `stopped` (failed, needs_human, including CI still red on its open PR) leaves its workspace in `<data_dir>/workspaces/<run_id>`, still holding the task branch and any unpushed commits; while it stays, new runs on that task use a suffixed branch (`<branch>-2`, ...) and a new PR. Deleting the folder frees the branch, and the next run resets it to the remote. Plain `uv run fact dev` runs from the source tree for developing fact by hand.

---

## Agent skills

### Issue tracker

Tickets are GitHub issues on `Kahncode/fact`, specced in the issue body: leaf issues are created `fact:ready`, parent issues `fact:planned` (the parent's label gates its sub-issues). See `docs/agents/issue-tracker.md`.

### Triage labels

The five triage roles map onto fact's own labels (`fact:planned`, `fact:ready`), assignment and close reasons. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context, with `docs/spec/05-domain-model.md` as the glossary and the decisions files as the ADR log. See `docs/agents/domain.md`.

---

## Project-specific rules

- Python 3.15, uv. Run things with `uv run ...`.
- No backwards compatibility before 1.0: databases are reset often, so don't fix up stored data, keep aliases or deprecated names, or add compatibility shims for old payloads, APIs or configs. Schema changes still get a new migration (past migrations stay untouched).
- Every script is Python. Never bash, never make, no `.sh` or `.bat` files.
- It must run on Windows, macOS and Linux: use `pathlib`, no shell-specific syntax, and run subprocesses with argument lists, not shell strings.
- Plain, mainstream code over clever abstractions: plain SQLAlchemy 2 async ORM, plain FastAPI routers, Pydantic v2 models. Don't build frameworks.
- Temporal workflow code is deterministic: no I/O, clock or randomness in workflows. All side effects go in activities.
- Runs must survive a worker or process restart. Never keep state a run's correctness or coordination depends on in process memory (no per-run lock dicts like `defaultdict(asyncio.Lock)`, no in-memory registries keyed by run, no assuming a retry lands on the same process). Keep it in Temporal, the database, the artifact store or on disk (git's state in the worktree); coordinate with heartbeats and heartbeat timeouts, `WAIT_CANCELLED`, idempotent activities that resume partial work, and the external system's own locks (git `index.lock`). A process-local cache is fine only if losing it changes nothing but speed.
- Never put logs, transcripts or diffs in Temporal payloads. Store them in the artifact store and pass references.
- Secrets live only in `.env` (git-ignored). Never commit keys or tokens.

---

## Critical rules

### 1. Ask before deploying

- Never auto-deploy, even if the fix seems simple.
- Wait for explicit confirmation ("yes, deploy this").

### 2. Quality gates

- No file over 300 lines; split if larger.
- No function over 50 lines; extract helpers.
- `ruff`, `black`, `lint-imports` and `pytest` must pass before committing.

### 3. Git workflow

Branch first, before editing any files (planning sessions that only edit docs follow `docs/agents/domain.md` instead: they commit on the `planning` branch, rebase on `origin/main` and push straight to `main`):

```bash
git checkout -b feat/<task-name>
```

- `feat/<name>`: new features
- `fix/<name>`: bug fixes
- `docs/<name>`: documentation
- `refactor/<name>`: refactors

Commit messages and PR titles follow [Conventional Commits](https://www.conventionalcommits.org): `type(scope): subject`. The scope names the area of the code the change touches (a package, module or component, such as `feat(worker): ...`, `fix(sdk): ...`, `docs(spec): ...`), using the scopes `git log` already uses; leave it out when the change spans several areas, and never use the task or issue id: the PR body links the issue. Tests go in the same commit as the code they test.

Before you commit:

```bash
uv run ruff check . --fix
uv run black .
uv run lint-imports
uv run pytest
```

All four must pass. Then update the context: this file, `docs/spec/` and the package READMEs, wherever your change made them wrong.

### ALWAYS DO

- Read `docs/spec/README.md` first and only the sections your task points to.
- Treat `docs/spec/` as the target application, not the code today: parts of it may not be built yet, and the code is the truth for what exists. Your issue may have been written before the spec was last updated; where they disagree, follow the spec for the design and the issue for what to build in this task. Never build spec beyond your task's scope, and never change the spec to match the code.
- Keep `docs/spec/` and package READMEs in sync with significant changes.
- Ask before refactoring more than 50 lines at once.
- Run ruff, black, lint-imports and pytest before committing.

### NEVER DO

- Never auto-commit changes without asking.
- Never modify past database migrations.
- Never suggest multi-file changes without explaining the refactoring strategy.
