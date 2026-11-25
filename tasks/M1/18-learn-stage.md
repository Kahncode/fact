---
id: M1-18
title: "Learn stage and agent-written PR title"
type: feature
complexity: standard
status: planned
depends_on: [M1-17]
human_actions: []
---

## Goal

After review, a `learn` session brings `CLAUDE.md`, `docs/spec/` and READMEs in line with the change on the same branch, and writes the PR title and summary that open the PR description.

## Read first

- `CLAUDE.md`
- `docs/spec/06-workflow-authoring.md` (Learn)
- `docs/spec/09-runners-and-harnesses.md` (Execution zones: agents write the words)
- `.fact/workflows/standard.py`
- `.agents/skills/learn/SKILL.md`

## Do

- Extend the `learn` skill (`.agents/skills/learn/SKILL.md`, and the `documenter` agent it delegates to), keeping the interactive flow (confirm before significant writes):
  - unattended mode, selected when explicit inputs or an output path are given: no confirmation, no commit unless told to;
  - optional output path: writes `{pr_title, pr_summary}` (a conventional-commit title for the whole change, and a few sentences on what changed and why);
  - works from a diff range, a task file or a plain-text fact.
- `.fact/prompts/learn.md` uses the learn skill (unattended, with the diff range and output path), commits its changes, and returns `title` and `summary` in its structured answer (`title` declared in `.fact/prompts/learn.schema.json`, like `implement`).
- `Standard`: a `learn` stage after review (and address review), before marking the task done. `open_change_set` takes the title and summary from that output, falling back to the task title and the implement summary.
- Tests: the workflow passes the title through; the fallback works when the output is missing.

## Out of scope

- Context outside the repository (future ideas).

## Acceptance criteria

- [ ] `uv run pytest` passes.
- [ ] `/learn` with no argument still works interactively; given a diff range and an output path it updates the docs without asking and writes `{pr_title, pr_summary}`.
- [ ] Run by fact, this task's PR title comes from the agent, not the task file.
