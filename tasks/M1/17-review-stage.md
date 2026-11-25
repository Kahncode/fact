---
id: M1-17
title: "Agent review and address-review stages"
type: feature
complexity: standard
status: planned
depends_on: [M1-16]
human_actions: []
---

## Goal

Before any PR, a separate review session checks the change against the task's acceptance criteria and the project's review checklist. Findings go to a fresh address-review session, followed by the check loop again.

## Read first

- `CLAUDE.md`
- `docs/spec/06-workflow-authoring.md` (Steps and acceptance; review checklist row)
- `docs/spec/08-planning.md` ("Acceptance criteria are the contract")
- `.fact/workflows/standard.py`, `.fact/prompts/`
- `.agents/skills/review/SKILL.md`

## Do

- Extend the `review` skill (`.agents/skills/review/SKILL.md`, and the `code-reviewer` agent it delegates to), keeping the interactive behaviour (no argument: review the current changes, numbered CRITICAL, WARNING or INFO findings):
  - optional inputs: the task file or a list of acceptance criteria, and a diff range; with criteria it also answers each one as met or not met, with evidence;
  - optional output path: writes the `review.json` below, mapping CRITICAL to high, WARNING to medium, INFO to low;
  - never changes code.
- `.fact/prompts/review.md` uses the review skill, passing it the task file's acceptance criteria and the output path, and must not change code. It returns, as its structured answer (fields declared in `.fact/prompts/review.schema.json`, see `.fact/prompts/README.md`):
  - `criteria`: a list of `{text, met, evidence}`;
  - `findings`: a list of `{severity: low|medium|high, file, line, message}`.
- `.fact/prompts/address_review.md`: fix the findings and unmet criteria in `$context`, then commit.
- Routing: `*.review` uses a different model from implement (opus in `fact.yaml`).
- `Standard`, after the checks:
  1. `review`;
  2. if any criterion is unmet or any finding is medium or high, then `address_review` followed by `until_green`.

  One review round in v0. Stages and the manifest are updated.
- The PR body shows the criteria table from the review.
- Tests: the workflow with fake activities, for a clean review and for findings; a session that ends without a valid structured answer gives outcome `failed_error`.

## Out of scope

- A second review round, or LLM-judge scoring (v0.2).

## Acceptance criteria

- [ ] `uv run pytest` passes.
- [ ] `/review` with no argument still prints numbered findings with severities; given a task file and an output path it writes valid `review.json`.
- [ ] Run by fact, this task's PR shows the criteria table.
