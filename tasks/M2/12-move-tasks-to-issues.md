---
id: M2-12
title: "Move the remaining task files to GitHub issues"
type: feature
complexity: standard
status: planned
depends_on: [M2-11]
human_actions: 
  - "Review the dry run, then run the export for real"
  - "Approve switching fact's task source to github"
---

## Goal

fact's own remaining task files become GitHub issues with the same dependencies, and fact's project switches its task source to GitHub Issues. That ends the file bootstrap.

## Read first

- `CLAUDE.md`
- `tasks/README.md`
- `core/src/fact/core/tasks/files.py`, `github_issues.py`
- `docs/spec/12-integrations.md` (labels)

## Do

- `fact tasks export-github --repo Kahncode/fact [--dry-run]`, for every task that isn't done and has no `moved_to`:
  1. Create an issue titled `<id>: <title>` with the task's body and a hidden marker `<!-- fact:task <id> -->`, so a re-run finds it instead of creating a duplicate.
  2. Labels: `fact:ready` or `fact:planned` from the status, `type:*` and `complexity:*`.
  3. Milestone: the task's folder name, created if missing.
  4. Then, once every issue exists, add the blocked-by links through the REST dependencies API, plus `Depends on #n` lines.
  5. Set `moved_to: gh:Kahncode/fact#n` in each file. `FileTaskSource` ignores moved tasks.
- `--dry-run` prints the plan without writing anything.
- A separate commit switches `.fact/project.yaml` to `task_source: {kind: github, repo: Kahncode/fact}` and updates `tasks/README.md` and `CLAUDE.md` to say new work goes to GitHub Issues.
- Tests with mocked GitHub: idempotent re-run, dependency mapping, dry run.

## Out of scope

- Deleting the old files: keep them as history.

## Acceptance criteria

- [ ] `uv run pytest` passes.
- [ ] After the real export, every open task is an issue with the right blocked-by links, and the scheduler picks up the next `fact:ready` issue.
