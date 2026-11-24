---
id: M2-05
title: "Keep a run open until its PR merges or closes"
type: feature
complexity: standard
status: planned
depends_on: [M2-04]
human_actions: []
---

## Goal

A run ends when its PR is merged or closed, not when the PR opens. Its outcome (`merged` or `closed`) and the time spent waiting for review are recorded, and the workspace is cleaned up.

## Read first

- `CLAUDE.md`
- `docs/spec/06-workflow-authoring.md` (Standard workflow code: `comments_until_merged`)
- `docs/spec/10-observability.md` (Outcome)
- `.fact/workflows/standard.py`

## Do

- A `wait_for_pr_closed(prs)` control activity that polls each PR every 60 s, heartbeating. It has a long start-to-close timeout (30 days) with retries and returns `merged` or `closed` per PR.
- `Standard`, after CI is green: notify that PRs are ready (M2-03), wait, set the run outcome, then tear the workspace down (a `teardown_workspace` control activity). The wait is recorded as human wait time on a `review` gate-like step.
- Tests: merged and closed paths with mocked GitHub; teardown is called in both.

## Out of scope

- `@fact` comments while waiting (M4).
- Auto-merge (M4).

## Acceptance criteria

- [ ] `uv run pytest` passes.
- [ ] Run by fact, this task's own run ends as `merged` after Samuel merges it.
