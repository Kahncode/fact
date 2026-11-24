---
id: M2-11
title: "GitHub Issues write-back"
type: feature
complexity: standard
status: planned
depends_on: [M2-09, M2-10, M2-03]
human_actions: []
---

## Goal

A GitHub issue shows what fact is doing. Labels track the run (`fact:running`, `fact:needs-human`), comments link the run and the PRs, fact's context lives between markers in the body, and the issue is closed when the change set merges.

## Read first

- `CLAUDE.md`
- `docs/spec/12-integrations.md` (GitHub Issues: Status, Factory context, Closing)
- `core/src/fact/core/tasks/github_issues.py`, `base.py`
- The `report_status` activity

## Do

- `GitHubIssuesSource`:
  - `comment`, `set_labels`;
  - `write_context`: replace the text between `<!-- fact:context:start -->` and `<!-- fact:context:end -->`, appending the markers if missing, and never touching your own text;
  - `link_prs`: a comment listing the PRs;
  - `close`: close as completed.
- Events, sent through `report_status` from `FactWorkflow` and `Standard`:
  - `run_started`: add `fact:running` and comment with the run link;
  - `gate_opened`: add `fact:needs-human` and comment with what is needed;
  - `gate_answered`: remove `fact:needs-human`;
  - `prs_opened`: post the PR links;
  - `merged`: close the issue and remove `fact:running`;
  - `closed` or `dropped`: remove `fact:running` and comment.
- Removing `fact:ready` does not stop a run (the spec says so; nothing to build).
- Tests with mocked GitHub for each event; markers when the body has none, one, or broken ones.

## Out of scope

- `@fact` comments on PRs (M4).

## Acceptance criteria

- [ ] `uv run pytest` passes.
- [ ] Your text outside the markers is kept byte for byte (test).
