---
id: M1-17
title: "Agent review and address-review stages"
type: feature
complexity: standard
status: planned
depends_on: [M1-16, M0-11]
human_actions: []
---

## Goal

Before any PR, a separate review session checks the change against the task's acceptance criteria and the project's review checklist. Findings go to a fresh address-review session, followed by the check loop again.

## Read first

- `CLAUDE.md`
- `docs/spec/06-workflow-authoring.md` (Steps and acceptance; review checklist row)
- `docs/spec/08-planning.md` ("Acceptance criteria are the contract")
- `.fact/workflows/standard.py`, `.fact/prompts/`
- `.claude/skills/review/SKILL.md`

## Do

- `.fact/prompts/review.md` uses the review skill and must not change code. It writes `.fact-out/review.json` with:
  - `criteria`: a list of `{text, met, evidence}`;
  - `findings`: a list of `{severity: low|medium|high, file, line, message}`.
- `.fact/prompts/address_review.md`: fix the findings and unmet criteria in `$context`, then commit.
- Routing: `*.review` uses a different model from implement (opus in `fact.yaml`).
- `Standard`, after the checks:
  1. `review`;
  2. if any criterion is unmet or any finding is medium or high, then `address_review` followed by `until_green`.

  One review round in v0. Stages and the manifest are updated.
- The PR body shows the criteria table from the review.
- Tests: the workflow with fake activities, for a clean review and for findings; the `review.json` schema is validated, and an invalid file gives outcome `failed_error`.

## Out of scope

- A second review round, or LLM-judge scoring (v0.2).

## Acceptance criteria

- [ ] `uv run pytest` passes.
- [ ] Run by fact, this task's PR shows the criteria table.
