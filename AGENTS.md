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
- **Decisions:** `docs/decisions.md`, implementation decisions (design decisions are in `docs/spec/16-decisions.md`).
- **Work:** `tasks/`, one markdown file per task, grouped by milestone. `tasks/README.md` explains the format. `tasks/backlog.md` holds ideas and problems not yet planned.

---

## Project structure

```
fact/
├── AGENTS.md, CLAUDE.md   instructions (CLAUDE.md is the @AGENTS.md shim)
├── fact.yaml              global settings and project registry
├── pyproject.toml         uv workspace root, ruff/black/pytest config
├── core/                  fact.core: settings, logging, DB models, migrations, artifact store, shared types
├── api/                   fact.api: FastAPI app
├── worker/                fact.worker: Temporal workers and activities
├── sdk/                   fact.sdk: step library and workflow base class that project workflows import
├── cli/                   fact.cli: the `fact` Typer command
│   (each package: src/fact/<name>/ and tests/)
├── scripts/               Python dev scripts (task loop) and their tests/
├── web/                   React UI (from M3)
├── .fact/                 fact's own project config, workflows and prompts
├── docs/spec/             design, one file per section (start at README.md)
├── tasks/                 work items by milestone (M0..), plus backlog.md
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
| Tests          | pytest, pytest-asyncio (auto mode)             | Per-package `tests/`                                                       |

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
| Start a run for a task  | `uv run fact run task:<id>` (`--force-deps`, `--project`) |
| List runs               | `uv run fact runs` (`--project`, `--status`)    |
| Show a run and its steps | `uv run fact status <run id or workflow id>`   |
| Check GitHub App access | `uv run fact github check <owner/repo>`         |
| Create fact's labels on a repo | `uv run fact github labels sync <owner/repo>` |
| Lint (autofix)          | `uv run ruff check . --fix`                     |
| Format                  | `uv run black .`                                |
| Install git hooks       | `uv run pre-commit install --install-hooks`     |

**Prerequisites:** Python 3.15 and uv; the Temporal CLI (`temporal`) on PATH for `fact dev`. Copy `.env.example` to `.env` for local secrets.

---

## Key patterns and conventions

- Packages share the `fact.*` namespace; dependencies point inward: `api`, `worker`, `sdk`, `cli` depend on `core`, and `worker` also on `sdk`.
- Temporal: workflows are deterministic and orchestrate only; every side effect is an activity.
- Large data (logs, transcripts, diffs) goes to the artifact store; Temporal payloads carry references.
- Project workflows import the step library from `fact.sdk` and subclass its workflow base class (`fact.sdk.workflow.FactWorkflow`).
- Queue names, activity names and activity inputs live in `fact.core.activity_contract`; `fact.sdk` calls activities by name and never imports `fact.worker`.
- Workflow handler names for people (`answer_gate` update, `status` query, `steer` signal) and their models live in `fact.core.gates`; clients call them by name and never import `fact.sdk`. A subclass of `FactWorkflow` that defines `__init__` must call `super().__init__()`.
- `python -m fact.worker` loads each project's `.fact/workflows/` from a detached checkout of its default branch at `pinned_dir/projects/<project>/<sha>` and runs them on queue and deployment `wf-<project>` (`workflow_queue`), pinned to build id `<factory SHA[:12]>+<home SHA>`; workflow changes apply at the next worker restart (see `worker/README.md`).
- Each package owns its tests in its own `tests/`.
- Logging goes through `fact.core.logging` (structlog): call `configure_logging(env, level)` once at process start, get loggers with `get_logger(name)`, and wrap run/step work in `bind_run(run_id, step_id)` so every line carries `run_id`.
- Settings come only from `fact.core.settings.get_settings()`, which loads `fact.yaml`:
  - Priority, low to high: defaults, `fact.yaml`, `fact.<env>.yaml`, `.env`, `FACT_*` env vars with `__` nesting.
  - `FACT_CONFIG` points at another `fact.yaml`; `.env` is read from the same folder.
  - `${VAR}` in YAML expands from the environment or a file path.
  - Settings are frozen; an app may subclass `Settings` via `override_settings_class`.
  - Clients (worker, CLI) reach the API at `settings.api_url`; don't build the URL from `api.host`/`api.port` yourself.
- GitHub access goes through `fact.core.github`: one `GitHubApp` per process (its token caches are per instance), tokens from `installation_token(owner/repo)`, HTTP via `github_client`. Never log or print a token.

---

## Dogfooding

fact works on its own tasks, so the running factory is never the code an agent edits: it runs from a pinned checkout, and agents only work in worktrees under `workspaces_dir` (`<data_dir>/workspaces`). `LocalWorktreeRunner.prepare` refuses a workspace inside `pinned_dir` or inside the factory checkout that is running.

- **Pin a version:** `uv run fact use main` fetches `origin`, checks `origin/main` (or any branch, tag or SHA) out as a detached worktree at `<data_dir>/pinned/factory/<sha>`, runs `uv sync --locked` there and writes the SHA to `<data_dir>/pinned/current`.
- **Run fact on its own tasks:** `uv run fact dev --pinned` starts Temporal here and the API and worker from the pinned version (the checkout's own `.venv` Python, without the venv activation `fact dev` was started with), still reading this tree's `fact.yaml` and `.env` through `FACT_CONFIG`. Then `uv run fact run task:<id>` and review the PR it opens. After merging fact changes, run `fact use main` again and restart.
- **If a run goes wrong:** stop `fact dev` (Ctrl-C), fix or finish the task by hand with Claude Code (the `next-task` skill) in this source tree, and pin the new version with `fact use main` once the fix is merged. A run that does not end `pr_opened` (failed, needs_human, including CI still red on its open PR, cancelled) leaves its workspace in `<data_dir>/workspaces/<run_id>`, still holding the task branch and any unpushed commits; while it stays, new runs on that task use a suffixed branch (`<branch>-2`, ...) and a new PR. Deleting the folder frees the branch, and the next run resets it to the remote. Plain `uv run fact dev` runs from the source tree for developing fact by hand.

---

## Project-specific rules

- Python 3.15, uv. Run things with `uv run ...`.
- Every script is Python. Never bash, never make, no `.sh` or `.bat` files.
- It must run on Windows, macOS and Linux: use `pathlib`, no shell-specific syntax, and run subprocesses with argument lists, not shell strings.
- Plain, mainstream code over clever abstractions: plain SQLAlchemy 2 async ORM, plain FastAPI routers, Pydantic v2 models. Don't build frameworks.
- Temporal workflow code is deterministic: no I/O, clock or randomness in workflows. All side effects go in activities.
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
- `ruff`, `black` and `pytest` must pass before committing.

### 3. Git workflow

Branch first, before editing any files:

```bash
git checkout -b feat/<task-name>
```

- `feat/<name>`: new features
- `fix/<name>`: bug fixes
- `docs/<name>`: documentation
- `refactor/<name>`: refactors

Commit messages and PR titles follow [Conventional Commits](https://www.conventionalcommits.org): `type(scope): subject`. Tests go in the same commit as the code they test.

Before you commit:

```bash
uv run ruff check . --fix
uv run black .
uv run pytest
```

All three must pass. Then update the context: this file, `docs/spec/` and the package READMEs, wherever your change made them wrong.

### ALWAYS DO

- Read `docs/spec/README.md` first and only the sections your task points to.
- Keep `docs/spec/` and package READMEs in sync with significant changes.
- Ask before refactoring more than 50 lines at once.
- Run ruff, black and pytest before committing.

### NEVER DO

- Never auto-commit changes without asking.
- Never modify past database migrations.
- Never suggest multi-file changes without explaining the refactoring strategy.
