---
id: M1-14
title: "Start a run: API, fact run and fact status"
type: feature
complexity: standard
status: planned
depends_on: [M1-13]
human_actions: []
---

## Goal

`fact run task:<id>` starts the project's workflow for a task through the API, after checking its dependencies. `fact status` shows a run and its steps.

## Read first

- `CLAUDE.md`
- `docs/spec/05-domain-model.md` (Run: workflow id format)
- `core/src/fact/core/tasks/files.py`
- `api/src/fact/api/routers/runs.py`
- `cli/src/fact/cli/main.py`

## Do

- `POST /api/v1/runs/start` with body `{task_key, project?, override_deps?: bool}`:
  1. Resolve the project (task keys map to the project whose task source holds them).
  2. Read the task with `FileTaskSource` from the project's local path, as it is on `origin/<default_branch>` (`git show`), not the working tree.
  3. Return 409 listing missing dependencies, unless `override_deps` is set.
  4. Workflow id `run-<key, safe form>-<n>`, where `n` is the next number for that task.
  5. Start the project's workflow type (`workflows.default`) on `wf-<project>`.
  6. Create the run row as `queued`.
  7. Return the run id, workflow id and the Temporal UI link.
- CLI:
  - `fact run <task_key> [--force-deps]`;
  - `fact runs [--project] [--status]`;
  - `fact status <run>`: the run with steps, durations, outcome, cost and log links.
- Tests: dependency refusal; workflow id numbering; start against a time-skipping Temporal environment with a stub workflow.

## Out of scope

- Automatic pickup of ready tasks (M2-07).
- Live status from a workflow query (M2-01).

## Acceptance criteria

- [ ] `uv run pytest api cli` passes.
- [ ] `uv run fact run task:M0-01` on Samuel's machine starts a run visible in `fact runs` and in the Temporal UI. It may fail later in the run; that's what M1-15 and M1-16 are for.
