---
id: M1-05
title: "Step reporter used by activities"
type: feature
complexity: standard
status: planned
depends_on: [M1-04]
human_actions: []
---

## Goal

Activities report a step with one context manager that creates the step row, binds `run_id` and `step_id` to logs, captures the step's output into a log file, uploads it, and records the outcome and usage. Every step is then observable the same way.

## Read first

- `CLAUDE.md`
- `docs/spec/10-observability.md`
- The API routers from M1-03 and M1-04
- `core/src/fact/core/logging.py`

## Do

- `worker/src/fact/worker/reporting.py`, with `StepReporter(api_base_url, http_client)`:
  - `async with reporter.step(run_id, stage_id, kind, attempt, harness=None, model=None) as s:` creates the step, binds logging, and opens a local log file;
  - `s.log(line)` appends to that file;
  - `s.event(type, payload)` buffers events, flushed in batches;
  - `s.attach(name, path)` uploads an artifact and returns its key;
  - `s.finish(outcome, summary, outputs, usage)`.

  On exit the reporter uploads `output.log`, patches the step, and returns a `StepResult`. If the body raises, the outcome is `failed_error` and the exception is re-raised.
- A reporting failure (API down) is logged and does not fail the step: steps must not depend on the API being up. Keep the log locally and retry the upload once at the end.
- Tests use the real API app through `ASGITransport` and a temporary data dir: a passing step, a raising step, and the API being unreachable.

## Out of scope

- What steps actually do (M1-07, M1-09).

## Acceptance criteria

- [ ] `uv run pytest worker` passes.
- [ ] After a reported step, `GET /runs/{id}` shows the step with its duration and `log_ref`, and `GET /artifacts/<log_ref>` returns the lines logged (test).
