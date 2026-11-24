---
id: M1-02
title: "File task source"
type: feature
complexity: standard
status: planned
depends_on: [M1-01]
human_actions: []
---

## Goal

fact can read the task files in `tasks/`: parse them into `Task`s, check their dependencies, list the ready ones, and mark one done on a branch. It also validates the whole set, so a broken task file fails CI.

## Read first

- `CLAUDE.md`
- `tasks/README.md` (the format and lifecycle you are implementing)
- `docs/spec/12-integrations.md` (Task source interface; issue keys)
- `core/src/fact/core/types.py`

## Do

- `core/src/fact/core/tasks/files.py`, with a `FileTaskSource(root: Path)` class:
  - `all()`: parse every `tasks/*/NN-*.md` (YAML front matter with PyYAML, then the body). Skip `README.md` and `backlog.md`, and tasks with `moved_to` set.
  - `get(key)`, where keys look like `task:M1-02`.
  - `deps_done(task)`.
  - `ready()`: tasks with status `ready` whose dependencies are all `done`.
  - `validate()`: ids unique and matching their file names, every `depends_on` id exists, no cycles, every status valid. Returns a list of errors.
- Acceptance criteria are parsed from the `## Acceptance criteria` checklist.
- `mark_done(worktree: Path, key)` rewrites only the `status:` line of that task's file to `done`, keeping everything else byte for byte.
- `branch_name(key)`: `task:M1-02` becomes `fact/task-m1-02`.
- Tests:
  - fixture task folders: ready, blocked, a cycle, a missing dependency;
  - `mark_done` keeps the rest of the file unchanged;
  - **a test that runs `validate()` on this repository's real `tasks/` and expects no errors.**

## Out of scope

- The general `TaskSource` interface and GitHub Issues (M2-08, M2-09).
- Starting runs (M1-14).

## Acceptance criteria

- [ ] `uv run pytest core` passes, including validation of the real `tasks/` folder.
- [ ] Introducing a dependency cycle in a task file makes that test fail with a message naming both ids.
