---
id: M1-07
title: "prepare_workspace and shell_step activities"
type: feature
complexity: standard
status: planned
depends_on: [M1-05, M1-06]
human_actions: []
---

## Goal

Workflows can prepare a run's workspace and run the project's own lint and test commands in it as reported, heartbeating Temporal activities whose typed outcome a workflow can branch on.

## Read first

- `CLAUDE.md`
- `docs/spec/06-workflow-authoring.md` (Steps and acceptance: Checks)
- `docs/spec/09-runners-and-harnesses.md` (Long agent steps: heartbeats)
- `worker/src/fact/worker/reporting.py`, `runners/local_worktree.py`
- `core/src/fact/core/project_config.py`

## Do

- `worker/src/fact/worker/activities/workspace.py`: a `prepare_workspace(spec) -> WorkspaceHandle` activity, running in the control zone and reported as kind `shell`, stage `prepare`.
- `worker/src/fact/worker/activities/shell.py`: a `shell_step(input) -> StepResult` activity (input: `run_id`, `stage_id`, `attempt`, `handle`, `repo`, `check` such as `lint` or `test`, `timeout_s`).
  - It reads `.fact/repo.yaml` from that repo's worktree, so a branch's own command changes apply.
  - It runs each command in order and stops at the first failure.
  - Every output line goes to the step log.
  - It heartbeats every 30 s.
  - The outcome is `passed` or `failed_checks`.
  - `summary` holds the last 4 KB of the failing command's output, for the fix prompt.
- Register both on the worker: `prepare_workspace` on `control`, `shell_step` on a new `sandbox` task queue. `python -m fact.worker` runs both queues.
- Tests use `ActivityEnvironment`, a temporary repo with a passing and a failing `repo.yaml`, and the API through `ASGITransport`.

## Out of scope

- Agent steps (M1-09).
- Retries and loops (workflow code, M1-12).

## Acceptance criteria

- [ ] `uv run pytest worker` passes.
- [ ] A failing test command yields `failed_checks` with the failure in `summary` and the full output under `log_ref` (test).
