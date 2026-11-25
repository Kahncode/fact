---
id: M1-16
title: "Cost comment on the PR (first task run by fact)"
type: feature
complexity: trivial
status: done
depends_on: [M1-15]
human_actions: 
  - "Run this task with fact itself: fact use main, fact dev --pinned, fact run task:M1-16"
  - "Write everything that broke or felt wrong into tasks/backlog.md"
---

## Goal

Every PR fact opens gets a comment with time and cost per step and per model. This is also the first task fact builds for itself: if it gets through to a PR, M1's thin end-to-end path works.

## Read first

- `CLAUDE.md`
- `docs/spec/06-workflow-authoring.md` (Review evidence: Cost comment)
- `worker/src/fact/worker/activities/` (`open_change_set`)
- `.fact/workflows/standard.py`

## Do

- A `post_cost_comment(run_id, prs)` control activity:
  - read the run and its steps from the API;
  - render a markdown table: stage, attempt, harness and model, duration, wait, tokens, cost; then totals;
  - create the comment, or update fact's existing one, found by a `<!-- fact:cost -->` marker.
- `Standard` calls it right after `open_change_set`.
- Tests: rendering from a fixture run; create versus update against a mocked GitHub API.

## Out of scope

- The Costs view (M6).

## Acceptance criteria

- [ ] `uv run pytest worker` passes.
- [ ] This task's own PR was opened by fact[bot] and carries the cost comment.
