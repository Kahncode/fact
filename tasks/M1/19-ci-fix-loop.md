---
id: M1-19
title: "Wait for CI and the CI fix loop"
type: feature
complexity: standard
status: planned
depends_on: [M1-18]
human_actions: []
---

## Goal

After opening the PR, fact waits for CI on the PR's head commit. Failures go to a fresh fix session with the failing logs, up to two times, and a run only reports success when CI is green.

## Read first

- `CLAUDE.md`
- `docs/spec/12-integrations.md` (GitHub: CI)
- `docs/spec/06-workflow-authoring.md` (the Standard workflow code: `until_ci_green`)
- `worker/src/fact/worker/activities/` (`open_change_set`, `post_cost_comment`)

## Do

- A `wait_for_ci(pr)` control activity:
  - poll check runs and commit statuses for the PR's head SHA every 30 s, heartbeating, until all are complete or 60 minutes pass;
  - outcome `passed` or `failed_checks`;
  - for failed GitHub Actions jobs, download the job logs into the artifact store and put the last 4 KB of each in `summary`.
- `FactWorkflow.until_ci_green(prs, fix_prompt, max_attempts)`: on failure, a `fix_ci` agent session with the failures as context, then `open_change_set` again (push and update the PR), then wait again. When exhausted, outcome `needs_human`.
- `.fact/prompts/fix_ci.md`.
- `Standard` calls it after the cost comment. The cost comment is updated at the end of the run.
- Tests: the activity against mocked check-run responses (pending then success, failure with logs); the workflow loop with fake activities.

## Out of scope

- Classification and auto-merge (M4).
- `@fact` comments (M4).

## Acceptance criteria

- [ ] `uv run pytest` passes.
- [ ] Run by fact, this task's PR shows a `wait_for_ci` step that waited for the real CI result.
