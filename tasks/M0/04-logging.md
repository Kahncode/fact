---
id: M0-04
title: "Structured logging with run_id binding"
type: feature
complexity: standard
status: planned
depends_on: [M0-01]
human_actions: []
---

## Goal

Every process logs through structlog: readable console output locally, JSON otherwise, and a `run_id` and `step_id` bound to every line when code runs inside a run.

## Read first

- `CLAUDE.md`
- `docs/spec/03-temporal.md` (Internal conventions: one run_id on every line)
- The template's `shared/core/logging.py` and `shared/core/request_context.py` (https://github.com/Kahncode/py-fastapi-template)

## Do

- `fact/core/logging.py`:
  - `configure_logging(env, level="INFO")`: structlog with contextvars merging, ISO timestamps and log level. Console renderer when `env` is `local` or `test`, JSON renderer otherwise. Route stdlib `logging` (uvicorn, temporalio) through the same processors.
  - `bind_run(run_id, step_id=None)`, a context manager that binds and then unbinds those keys.
  - `get_logger(name)`.
- Keep it small: copy only what fact needs from the template, with no GCP logging.
- Tests: the JSON output contains `run_id` inside `bind_run` and not after it; stdlib log records come out through structlog.

## Out of scope

- Request-id middleware for the API (M0-08).
- OpenTelemetry (later).

## Acceptance criteria

- [ ] `uv run pytest core` passes, including the two tests above.
- [ ] No GCP or cloud-specific code is copied.
