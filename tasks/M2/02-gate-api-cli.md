---
id: M2-02
title: "API and CLI for gates, steering and status"
type: feature
complexity: standard
status: planned
depends_on: [M2-01]
human_actions: []
---

## Goal

You can answer gates, steer a run and see its live status from the CLI, through the API, which talks to Temporal. This is the human side of M2-01 until the UI exists.

## Read first

- `CLAUDE.md`
- `docs/spec/03-temporal.md` (Internal conventions)
- `api/src/fact/api/routers/runs.py`
- `cli/src/fact/cli/main.py`
- `sdk/src/fact/sdk/workflow.py` (the update, query and signal names)

## Do

- API:
  - `POST /api/v1/runs/{id}/gates/{gate_id}` with `{decision, note}` runs the update. It returns 409 with the validator's message when the gate is not open.
  - `POST /api/v1/runs/{id}/instructions` with `{text}` sends the signal.
  - `GET /api/v1/runs/{id}/status` runs the query.

  Each answer and instruction adds one to the run's `human_touches`.
- CLI:
  - `fact gate <run> approve|reject [--note TEXT]`;
  - `fact steer <run> TEXT`;
  - `fact status <run>` now also shows the open gate from the query.
- Tests against a time-skipping environment running a small workflow that opens one gate.

## Out of scope

- Notifications and the inbox (M2-03).
- The UI (M3).

## Acceptance criteria

- [ ] `uv run pytest api cli` passes.
- [ ] `fact gate <run> approve` on a run waiting at a gate moves it on (test), and a second approve returns the 409 message.
