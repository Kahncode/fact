# fact

fact is a software factory: a local service that turns tasks into reviewed GitHub PRs using coding agents, orchestrated by Temporal. Its first project is itself.

## Where things are

- `docs/spec/`: the design, one file per section. Start from `docs/spec/README.md` and read only what your task points to.
- `tasks/`: the work, one markdown file per task, grouped by milestone. `tasks/README.md` explains the format.
- `tasks/backlog.md`: ideas and problems not yet planned.
- Python packages (a uv workspace, namespace `fact.*`), each with its own `tests/`:
  - `core/`: settings, logging, DB models and migrations, artifact store, shared types (`fact.core`)
  - `api/`: FastAPI app (`fact.api`)
  - `worker/`: Temporal workers and activities (`fact.worker`)
  - `sdk/`: the step library and workflow base class that project workflows import (`fact.sdk`)
  - `cli/`: the `fact` command, Typer (`fact.cli`)
- `web/`: the React UI (from M3).
- `fact.yaml`: global settings and the project registry. `.fact/`: fact's own project config, workflows and prompts.

Some of these folders appear only as the M0 tasks create them.

## Rules

- Python 3.15, uv. Run things with `uv run ...`.
- Every script is Python. Never bash, never make, no `.sh` or `.bat` files.
- It must run on Windows, macOS and Linux: use `pathlib`, no shell-specific syntax, and run subprocesses with argument lists, not shell strings.
- Plain, mainstream code over clever abstractions: plain SQLAlchemy 2 async ORM, plain FastAPI routers, Pydantic v2 models. Don't build frameworks.
- Temporal workflow code is deterministic: no I/O, clock or randomness in workflows. All side effects go in activities.
- Never put logs, transcripts or diffs in Temporal payloads. Store them in the artifact store and pass references.
- Secrets live only in `.env` (git-ignored). Never commit keys or tokens.
- Tests go in the same commit as the code they test.

## Before you commit

```
uv run ruff check . --fix
uv run black .
uv run pytest
```

All three must pass. Then update the context: this file, `docs/spec/` and the package READMEs, wherever your change made them wrong.
