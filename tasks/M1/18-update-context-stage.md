---
id: M1-18
title: "Update-context stage and agent-written PR title"
type: feature
complexity: standard
status: planned
depends_on: [M1-17]
human_actions: []
---

## Goal

After review, an update-context session brings `CLAUDE.md`, `docs/spec/` and READMEs in line with the change on the same branch, and writes the PR title and summary that open the PR description.

## Read first

- `CLAUDE.md`
- `docs/spec/06-workflow-authoring.md` (Update context)
- `docs/spec/09-runners-and-harnesses.md` (Execution zones: agents write the words)
- `.fact/workflows/standard.py`
- `.claude/skills/update-context/SKILL.md`

## Do

- `.fact/prompts/update_context.md` uses the update-context skill, commits its changes, and writes `.fact-out/update_context.json` with `{pr_title, pr_summary}`.
- `Standard`: an `update_context` stage after review (and address review), before marking the task done. `open_change_set` takes the title and summary from that output, falling back to the task title and the implement summary.
- Tests: the workflow passes the title through; the fallback works when the output is missing.

## Out of scope

- Context outside the repository (future ideas).

## Acceptance criteria

- [ ] `uv run pytest` passes.
- [ ] Run by fact, this task's PR title comes from the agent, not the task file.
