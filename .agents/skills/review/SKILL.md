---
name: review
description: Review code changes for bugs, security issues, and best practices, and optionally check them against a task's acceptance criteria and write review.json. Use for code audits, security checks, quality reviews, or reviewing a change before its PR.
scope: project
allowed-tools: Read, Grep, Glob, Bash, Write
---

# Code Review

Warn if on `main` or `master`, then delegate to the `code-reviewer` agent. The review never changes code.

Current branch: !`git branch --show-current`

## Inputs (all optional)

Parse the user's argument for these, in any order:

- **Diff range or ref**: e.g. `HEAD~3..HEAD`, `origin/main...HEAD`, `bd-73`. Without one the agent picks what to review (staged → unstaged → branch vs main).
- **Acceptance criteria**: a task file (e.g. `tasks/M1/17-review-stage.md`; take its `## Acceptance criteria` checklist) or a list of criteria given inline.
- **Output path**: a path ending in `.json` (e.g. `review.json`, `out=../review.json`).

Pass the agent the diff range, the criteria as a numbered list (read the task file yourself if one was given), and the output path.

## Output

With no argument, the agent prints its usual report: numbered findings, each `CRITICAL`, `WARNING` or `INFO`, and a summary.

With criteria, the report also answers each criterion as met or not met, with evidence.

With an output path, the agent also writes this JSON there (and nothing else to disk):

```json
{
  "criteria": [{ "text": "uv run pytest passes", "met": true, "evidence": "312 passed" }],
  "findings": [{ "severity": "medium", "file": "sdk/src/fact/sdk/review.py", "line": 42, "message": "..." }]
}
```

- `criteria`: every given criterion, in order; `[]` when none were given.
- `findings`: every finding in the diff (not the UNRELATED block), with `severity` mapped CRITICAL → `high`, WARNING → `medium`, INFO → `low`; `file` relative to the repository root (`""` if none) and `line` an integer or `null`.

After the agent finishes, check that the file parses as JSON with exactly these fields and types; fix the file if it does not.
