---
id: M2-10
title: "Create fact's labels on a repository"
type: feature
complexity: trivial
status: planned
depends_on: [M1-10]
human_actions: []
---

## Goal

`fact github labels sync <owner/repo>` creates or updates every label fact uses, so a new task repository is ready in one command.

## Read first

- `CLAUDE.md`
- `docs/spec/12-integrations.md` (the labels table)
- `core/src/fact/core/github/client.py`

## Do

- A labels list in code, each with a colour and description:
  - `fact:ready`, `fact:planned`, `fact:running`, `fact:needs-human`;
  - `type:incident`, `type:bug`, `type:feature`, `type:chore`;
  - `complexity:trivial`, `complexity:standard`, `complexity:complex`.
- The sync creates the missing labels and updates colour and description on existing ones. It never deletes labels.
- Tests with mocked GitHub: create, update, idempotent re-run.

## Out of scope

- Setting labels on issues (M2-11).

## Acceptance criteria

- [ ] `uv run pytest` passes.
- [ ] Running it twice on Kahncode/fact makes no change the second time (checked by hand).
