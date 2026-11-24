---
id: M1-15
title: "Release checkout so fact can work on itself"
type: feature
complexity: standard
status: planned
depends_on: [M1-14]
human_actions: []
---

## Goal

The running factory is never the code an agent edits: fact runs from a release checkout at a fixed ref, and agents only ever work in worktrees under `workspaces_dir`.

## Read first

- `CLAUDE.md`
- `docs/spec/14-dogfooding-and-safety.md`
- `cli/src/fact/cli/dev.py` (or wherever `fact dev` lives)
- `worker/src/fact/worker/runners/local_worktree.py`

## Do

- `fact release <ref>`:
  1. fetch;
  2. create a detached worktree of the factory at `release_dir/factory/<sha>`;
  3. `uv sync --locked` in it;
  4. write `release_dir/current` with the SHA.
- `fact dev --release` runs the API and worker from the current release with `uv run --project <dir>`. Plain `fact dev` keeps running from the source tree for developing fact by hand.
- Guards in `LocalWorktreeRunner.prepare`: refuse a workspace inside `release_dir` or inside the factory checkout that is running.
- A "Dogfooding" section in `CLAUDE.md`: how to cut a release, how to run fact on its own tasks, and how to go back to Claude Code if a run goes wrong.
- Tests: the guard refuses those paths; the release command line is built correctly.

## Out of scope

- Running several factory versions side by side (later).

## Acceptance criteria

- [ ] `uv run pytest` passes.
- [ ] `uv run fact release main`, then `uv run fact dev --release`, runs fact from `release_dir/factory/<sha>` (checked by hand, noted in the PR).
