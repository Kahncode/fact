---
id: M2-03
title: "Notifications whenever a run waits for you"
type: feature
complexity: standard
status: planned
depends_on: [M2-02]
human_actions: []
---

## Goal

Every time a run starts waiting for a person, or fails, or has a PR ready, fact creates a notification. `fact inbox` lists them until the UI's Notifications page exists.

## Read first

- `CLAUDE.md`
- `docs/spec/11-ui-and-notifications.md` (Notifications)
- `core/src/fact/core/models.py`
- `sdk/src/fact/sdk/workflow.py` (`gate`)

## Do

- A `Notification` model and migration:
  - columns `id`, `run_id`, `kind` (`gate_waiting`, `prs_ready`, `run_failed`, `run_finished`), `title`, `body`, `link`, `created_at`, `read_at`, `dedupe_key`;
  - unique on `dedupe_key`, so repeats for the same run state are grouped.
- A `notify` control activity calling `POST /api/v1/notifications`. `FactWorkflow.gate()` calls it when a gate opens; `Standard` calls it when PRs are ready and when the run ends badly.
- API:
  - `GET /api/v1/notifications?unread=true`;
  - `POST /api/v1/notifications/{id}/read`.
- CLI: `fact inbox [--all]` and `fact inbox read <id>`.
- Tests: dedupe; the gate creates exactly one notification.

## Out of scope

- Desktop notifications and the Notifications page (M3).
- Slack and Discord (later).
- Quiet hours and reminders (later).

## Acceptance criteria

- [ ] `uv run pytest` passes.
- [ ] A run waiting at a gate shows up once in `fact inbox` (test).
