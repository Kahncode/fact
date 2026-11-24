---
id: M1-03
title: "API for runs, steps and events"
type: feature
complexity: standard
status: planned
depends_on: [M0-08, M1-01]
human_actions: []
---

## Goal

The worker reports everything that happens in a run through the API: run rows, step attempts with timing, usage and outcome, and events. The API keeps run totals up to date.

## Read first

- `CLAUDE.md`
- `docs/spec/10-observability.md`
- `core/src/fact/core/models.py`
- `api/src/fact/api/app.py` and one existing router

## Do

- Routers under `/api/v1`, in `api/src/fact/api/routers/runs.py`:
  - `POST /runs` creates a run (`workflow_id`, `project`, `task_key`, `workflow_type`, `complexity`, `worker_version`, `home_sha`).
  - `PATCH /runs/{id}` updates status, outcome and timestamps.
  - `GET /runs` (filter by project, status, task_key) and `GET /runs/{id}` (with its steps, ordered).
  - `POST /runs/{id}/steps` starts a step attempt (`stage_id`, `kind`, `attempt`, `queued_at`, `started_at`, `harness`, `model`).
  - `PATCH /steps/{id}` finishes it (`status`, `outcome`, `ended_at`, `wait_seconds`, usage, `log_ref`, `transcript_ref`, `outputs`).
  - `POST /runs/{id}/events` takes a batch of `{type, at, step_id, payload}`.
- When a step finishes, recompute the run's totals (machine seconds, human wait seconds, tokens, cost) from its steps.
- Request and response models are Pydantic. Unknown run or step ids return 404.
- Tests for each endpoint, plus totals after two finished steps.

## Out of scope

- Artifact upload (M1-04).
- Starting a workflow (M1-14).
- Live streaming to the UI (M3).

## Acceptance criteria

- [ ] `uv run pytest api` passes.
- [ ] After two steps finish with costs 0.10 and 0.25, `GET /runs/{id}` shows `cost_usd` 0.35 (test).
