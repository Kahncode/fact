---
id: M2-08
title: "Task source interface"
type: feature
complexity: standard
status: planned
depends_on: [M2-07]
human_actions: []
---

## Goal

Task ingestion goes through one interface, so the scheduler, `fact run` and the workflows don't care whether a task is a file or a GitHub issue.

## Read first

- `CLAUDE.md`
- `docs/spec/12-integrations.md` (Task source interface, issue keys)
- `core/src/fact/core/tasks/files.py`
- Callers of `FileTaskSource` (`grep -r FileTaskSource`)

## Do

- `core/src/fact/core/tasks/base.py`:
  - a `TaskSource` protocol with the spec's methods:
    - `list_ready()`;
    - `get(key)`;
    - `deps_done(task)`;
    - `comment(key, text)`;
    - `set_labels(key, add, remove)`;
    - `write_context(key, section)`;
    - `link_prs(key, prs)`;
    - `close(key)`;
  - `parse_key(key)` for `task:M1-02` and `gh:owner/repo#12`;
  - `branch_name(key)`;
  - `source_for(project_config, settings)`.
- `FileTaskSource` implements the protocol. The write-back methods only log, since a file task's state lives in the file and the DB.
- Move the scheduler, `fact run` and the workflows to the interface. A `report_status(task_key, event)` control activity dispatches status changes to the task's source.
- Tests: key parsing; every caller still passes its existing tests.

## Out of scope

- GitHub Issues itself (M2-09, M2-11).
- Linear (not scheduled).

## Acceptance criteria

- [ ] `uv run pytest` passes with no behaviour change.
- [ ] Nothing outside `core/tasks/` imports `FileTaskSource` directly (check with grep).
