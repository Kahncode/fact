---
id: M1-12
title: "Workflow base class, stage manifest and the Standard workflow"
type: feature
complexity: complex
status: planned
depends_on: [M1-02, M1-09, M1-11]
human_actions: []
---

## Goal

fact has a first real workflow. `fact.sdk` gives workflow authors a base class with `agent`, `until_green` and change-set helpers and a static stage manifest. fact's own `.fact/workflows/standard.py` uses it to go from task to PR: prepare, implement, a lint and test loop, mark the task done, open the PR.

## Read first

- `CLAUDE.md`
- `docs/spec/06-workflow-authoring.md` (step library, stage manifest, Steps and acceptance code)
- `docs/spec/03-temporal.md` (Determinism, conventions)
- `core/src/fact/core/types.py`
- The activity signatures in `worker/src/fact/worker/activities/`

## Do

- `sdk/src/fact/sdk/workflow.py`:
  - `Stage(id, label, kind, loops_to=None)`.
  - A `stages([...])` class decorator that stores the manifest on the class for the UI.
  - `FactWorkflow`, with these helpers:
    - `prepare()`;
    - `agent(stage_id, prompt, context="")`;
    - `until_green(checks, fix_prompt, max_attempts)`: run each check; on failure, a fresh fix agent session with the failure summary as context; repeat; on exhaustion return `failed_checks`;
    - `mark_task_done()`;
    - `open_change_set(title, summary)`.

    The helpers call activities by name (so `fact.sdk` does not import the worker), with the right task queue, timeouts from the input, and no Temporal retries for agent steps (retry only infrastructure errors on shell and control steps). Attempt numbers are tracked per stage.
- A `mark_task_done` control activity: `FileTaskSource.mark_done` on the worktree, then a commit `<id>: mark done`. PRs are squash-merged, so the PR becomes one commit.
- Control activities `record_run_started` and `record_run_finished`, calling the runs API.
- `.fact/workflows/standard.py`, a `Standard` workflow with stages `prepare`, `implement`, `checks`, `open_pr`. If the checks loop is exhausted, the run ends with outcome `needs_human` and no PR. M2 turns that into a gate.
- Tests with `WorkflowEnvironment.start_time_skipping()` and fake activities registered under the real names:
  - the green path;
  - two failures then green (three agent sessions);
  - exhausted (`needs_human`, no PR).

  A replay test re-runs the green path's history with `Replayer`, from a saved JSON history in `worker/tests/histories/` (the workflow file lives in `.fact/workflows/`, so its tests live in `worker/tests/`).

## Out of scope

- Loading workflows from the home repository at a fixed SHA (M1-13).
- Review, update context and CI (M1-17 to M1-19).
- Gates (M2).

## Acceptance criteria

- [ ] `uv run pytest sdk worker` passes, including the replay test.
- [ ] The workflow code does no I/O and reads no clock directly (review the diff).
- [ ] `Standard` exposes its stage manifest as data (test).
