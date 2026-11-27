# Storage and stack

Everything runs as three local processes (Temporal, the API, the worker) with two SQLite files; hosted (M5, `19-cloud.md`), the control plane runs on Railway with Supabase for the database, Auth and Storage: a connection string and a storage driver, not a rewrite. The table names the tech for each component.

| Component | v0 tech | Later |
| --- | --- | --- |
| Web UI | React + TypeScript, built with Vite; TanStack Router and TanStack Query; React Flow for the run graph; Tailwind CSS + shadcn/ui; live updates over SSE (EventSource); browser Notification API | Same, plus auth |
| Factory API | Built from [Kahncode/py-fastapi-template](https://github.com/Kahncode/py-fastapi-template): Python 3.15, FastAPI, Pydantic v2, Uvicorn, and the template's shared library (config, logging, middlewares, services); SSE via `sse-starlette`; serves the built UI as static files | Same, plus auth |
| MCP server (planning from Claude) | Official MCP Python SDK, mounted in the FastAPI app | Same |
| CLI (`fact run`, `fact upgrade`) | Typer, calling the API | Same |
| Orchestration engine | Temporal dev server (`temporal server start-dev`), SQLite file | Self-hosted Temporal on Railway (M5), on its own Railway Postgres; TLS and Temporal's JWT authorizer on the frontend so runners can poll from anywhere (`19-cloud.md`); Temporal Cloud rejected on cost |
| Worker (workflows and steps) | Temporal Python SDK (`temporalio`); steps run CLIs with `asyncio` subprocesses | A stateless control worker on Railway, and runner workers per pool wherever the compute is (M5: your PC) |
| Factory DB | SQLite with plain SQLAlchemy 2 async (aiosqlite driver) and Alembic, without the template's database service, so agents write standard SQLAlchemy; Postgres-compatible types only | Supabase Postgres (M5), through the session pooler |
| Logs and transcripts | A small artifact store interface with a local-folder backend in v0; the DB stores only links | Supabase Storage (M5), S3 API; runners upload through presigned URLs |
| Structured logging | `structlog`, set up as in the template (request id in logs and the `X-Request-ID` header, one log line per request, console locally, JSON elsewhere, optional Cloud Logging fields, per-env level), with `run_id` bound to every log line | OpenTelemetry export |
| Runner pool: your PC | `git clone`; Docker CLI for containers (v1) | GitHub-hosted runners, Daytona |
| Harnesses | Claude Code CLI (`claude -p --output-format stream-json`), Codex CLI (`codex exec --json`) | OpenCode and Pi adapters |
| Linear | GraphQL API over `httpx`, polling every 30 s | Webhooks |
| GitHub | `gh` CLI for git and PR operations, REST API over `httpx` for checks, labels and comments | GitHub App with webhooks (M5) |
| Config | YAML (factory settings in `fact.yaml`, the `fact dev` config in the factory repo, overridden per deployment; project config in `.fact/project.yaml` and `.fact/repo.yaml` in project repositories) loaded with pydantic-settings, with per-environment config files as in the template (local, dev, staging, prod); secrets in `.env` | A secret store |
| Tests | pytest, pytest-asyncio and pytest-postgresql from the template, plus Temporal's test environment and replay tests; for the UI as much testing as possible: Vitest and Testing Library for components and hooks against a mocked API (MSW), Playwright end-to-end scenarios for every page and critical flow (answer a gate, start a run, follow a live log) against a seeded API, plus ESLint, Prettier, `tsc` and the build in CI | Same |
| Dev tooling | `uv` (Python), `pnpm` (UI), Black, Ruff, pre-commit and detect-secrets from the template, import-linter for package boundaries; one cross-platform `fact dev` | Same |

Tooling: Python 3.15 with `uv`, `pnpm` for the UI, and one `fact dev` (a Python script; every script in fact is Python, never bash) that starts Temporal, the API and the worker.

Config lives in git, in two kinds that never mix: factory settings in `fact.yaml` in the factory repo (the machine and processes, the factory's concurrency cap, scheduler policy, accounts, notification rules, project registry; a deployment overrides it with `fact.<env>.yaml`, `FACT_CONFIG` and `FACT_*`), and project config in `.fact/` in each project's home repository (project settings, routing, step limits, its own concurrency cap, workflows, prompts); `.fact/repo.yaml` in each repository (its own commands).
