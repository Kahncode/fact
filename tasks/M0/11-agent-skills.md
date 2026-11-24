---
id: M0-11
title: "Commit the review, update-context and run-tests skills"
type: chore
complexity: trivial
status: planned
depends_on: [M0-01]
human_actions: 
  - "Provide your existing review, update-context and run-tests Claude Code skills (paste or copy them into .claude/skills/)"
---

## Goal

The Claude Code skills that fact's agent steps will call (review, update context, run tests) live in this repository, so headless runs use the same version on every machine.

## Read first

- `CLAUDE.md`
- The skill files Samuel provides
- `.claude/skills/do-task/SKILL.md`, for the format

## Do

- Put each skill in `.claude/skills/<name>/SKILL.md`: `review`, `update-context`, `run-tests`.
- Adapt them to this repository: commands go through `uv run`, the context files are `CLAUDE.md`, `docs/spec/` and package READMEs, and tests are run with `uv run pytest`.
- `review` must check every acceptance criterion of the task under review and write its findings as a list with a severity each.
- Mention the skills in `CLAUDE.md`.

## Out of scope

- The prompts that call these skills (M1-09, M1-17, M1-18).

## Acceptance criteria

- [ ] The three skills exist and Claude Code lists them in this repository.
- [ ] Running `/review` on a small branch produces findings that reference the task's acceptance criteria.
