---
id: M2-04
title: "Plan approval gate"
type: feature
complexity: standard
status: planned
depends_on: [M2-01]
human_actions: []
---

## Goal

A project or task can require you to approve the plan before execution starts, through the `require_plan_approval` switch. For file tasks, setting `status: ready` is already the approval, so fact's own project turns it off.

## Read first

- `CLAUDE.md`
- `docs/spec/08-planning.md` (Approve)
- `core/src/fact/core/project_config.py`
- `.fact/workflows/standard.py`

## Do

- `RunInput` gets `require_plan_approval`, taken from the task's own front matter if it sets it, otherwise from the project config.
- `Standard`: when it is on, an `approve_plan` gate comes first, with details holding the task title, the path or link, and the acceptance criteria. Approve continues; reject ends the run with outcome `dropped` and the note stored on the gate step.
- fact's `.fact/project.yaml` sets `require_plan_approval: false`. Document why in a comment.
- Tests: the gate is skipped when off; approve continues; reject drops the run.

## Out of scope

- Writing or editing plans (M5 planner).

## Acceptance criteria

- [ ] `uv run pytest` passes.
- [ ] The manifest shows `approve_plan` as a stage (test).
