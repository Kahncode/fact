# fact

fact is a software factory: a local service that turns tasks into reviewed GitHub PRs using coding agents, orchestrated by Temporal. Its first project is itself. The design is in `docs/spec/`, the work in `tasks/`.

## Setup

```
uv sync
```

Requires [uv](https://docs.astral.sh/uv/); it installs Python 3.15 if needed.

## Checks

```
uv run ruff check .
uv run black --check .
uv run pytest
uv run pre-commit run --all-files
```
