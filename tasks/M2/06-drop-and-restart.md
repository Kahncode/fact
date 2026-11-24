---
id: M2-06
title: "Drop a run and restart it on the latest version"
type: feature
complexity: standard
status: planned
depends_on: [M2-05]
human_actions: []
---

## Goal

You can drop a run and start the task again on the newest workflow version. The new run picks up the old branch and PR and gets a handoff note, so nothing is lost.

## Read first

- `CLAUDE.md`
- `docs/spec/06-workflow-authoring.md` ("Versions are pinned, with no upgrades")
- `api/src/fact/api/routers/runs.py`
- `worker/src/fact/worker/runners/local_worktree.py` (resume from an existing branch)

## Do

- `POST /api/v1/runs/{id}/drop` and `fact drop <run>`: cancel the workflow with a reason.
- On cancellation, `FactWorkflow` records outcome `dropped`, writes a handoff note to the artifact store and tears down the local worktree. It keeps the remote branch and the PR.
  - The handoff note holds the last stage reached, each agent step's summary, and the PR links.
- When starting a run for a task whose previous run was dropped, pass the newest handoff note's key in `RunInput`. The implement prompt gets it as `$handoff`, empty when there is none.
- Tests: drop mid-run; restart reuses the branch and the PR (mocked); the handoff reaches the prompt.

## Out of scope

- Doing this from the UI (M3).

## Acceptance criteria

- [ ] `uv run pytest` passes.
- [ ] A dropped run's PR stays open and is reused by the next run (test).
