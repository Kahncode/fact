---
id: M2-01
title: "Gates, status query and steering"
type: feature
complexity: complex
status: planned
depends_on: [M1-19]
human_actions: []
---

## Goal

A workflow can wait for a person: a gate is a Temporal update answered with approve or reject and a note, status is a query, and extra instructions are a signal added to the next agent prompt. When a check loop is exhausted, the Standard workflow now opens a gate instead of ending.

## Read first

- `CLAUDE.md`
- `docs/spec/03-temporal.md` (Internal conventions)
- `docs/spec/06-workflow-authoring.md` ("Long human waits are fine")
- `docs/spec/10-observability.md` (Human wait time)
- `sdk/src/fact/sdk/workflow.py`, `.fact/workflows/standard.py`

## Do

- In `FactWorkflow`:
  - `await self.gate(gate_id, kind, details) -> GateAnswer` opens a gate and waits.
  - The `answer_gate(GateAnswer{gate_id, decision: approve|reject, note})` update answers it. Its validator rejects unknown or already-closed gates with a clear error.
  - The `status()` query returns the current stage, attempt, open gate (id, kind, details, opened at) and the outcomes so far.
  - The `steer(text)` signal queues instructions; the next `agent()` call appends them to its context under "Additional instructions", then clears them.
- Gates are recorded as steps of kind `gate`, through control activities: opened, then answered with `wait_seconds`. The run status is `waiting` while a gate is open.
- `Standard`: when `until_green` or `until_ci_green` is exhausted, open a `needs_human` gate. Approve retries that loop once more; reject ends the run with `needs_human`.
- Tests (time-skipping environment, fake activities): approve, reject, an invalid gate id raising an error to the caller, steering reaching the next agent's context, and a wait of several days costing nothing.

## Out of scope

- API and CLI for answering (M2-02).
- Notifications (M2-03).
- The plan approval gate (M2-04).

## Acceptance criteria

- [ ] `uv run pytest sdk worker` passes, including the replay test re-recorded for the new workflow shape.
- [ ] Answering a closed gate returns an error, not a silent no-op (test).
