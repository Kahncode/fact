---
name: learn
description: Update project documentation from new knowledge. Accepts a git SHA, SHA range, a task file, a plain-text fact or decision, or no args to auto-detect from staged/unstaged/last commit; with an output path it also writes a PR title and summary.
scope: project
argument-hint: [sha | sha1..sha2 | tasks/<file>.md | message] [out=<path>.json] [unattended] [commit]
---

# Learn

Update project documentation based on what changed or what was learned.

**Source:** $ARGUMENTS

## Inputs

Parse `$ARGUMENTS` for these, in any order:

- **Source** (optional): a commit SHA, a range (`sha1..sha2`, `origin/main...HEAD`), a task file (`tasks/<milestone>/<file>.md`) or a plain-text fact or decision. See Source Resolution.
- **Output path** (optional): a path ending in `.json` (e.g. `pr.json`, `out=../pr.json`). See Output.
- **Flags** (optional): `unattended` (see Mode), `commit` (commit the doc updates when done).

## Mode

- **Interactive** (the default, whatever the source): confirm significant writes with the user, as below.
- **Unattended** (an output path or the `unattended` flag was given): nobody is there to answer. Don't ask anything: write the doc updates directly. When a fact conflicts with existing documentation, keep what the code shows and say so in your report. Don't commit unless the `commit` flag was given; otherwise the caller commits.

## Source Resolution

Resolve the source in this priority order:

### 1. Git SHA or range — `/learn <sha>` or `/learn <sha1>..<sha2>`

If the source looks like a commit SHA (7–40 hex chars), a ref, or a range (`a..b`, `a...b`):
- Run `git show <sha>` or `git log --patch <range>` (for `a...b`, `git diff a...b` plus `git log a..b` for the messages) to get the diff
- Include the SHA(s) in any Decisions log entries

### 2. Task file — `/learn tasks/M1/18-learn-stage.md`

If the source is a path to a task file: read it, and use its goal and "Do" section as the intent, with the diff from auto-detection below (or the range, when one was also given).

### 3. Plain-text fact or decision — `/learn <message>`

If the source is a human-readable statement:
- Treat it as a fact or decision to incorporate into docs
- In interactive mode, if it conflicts with existing documentation, surface the conflict and ask the user to resolve it before writing

### 4. No source — Auto-detect

Check in order:
1. **Staged changes** — `git diff --cached`: if any exist, use those
2. **Unstaged changes** — `git diff`: if any exist, use those
3. **Last commit** — `git log -1 --patch`: fallback if the working tree is clean

## Behavior

After resolving the source:

1. Identify which parts of the codebase changed or which facts are new
2. Determine which documentation files are affected
3. In interactive mode, **ask the user to confirm before writing significant changes** (more than a few lines). In unattended mode, write them.
4. Update only the docs relevant to what changed — do not rewrite unrelated sections. Always check `README.md` (written for humans, not agents) and update it when the change affects features, setup, usage, components, or structure
5. Append a Decisions entry for non-trivial changes, facts, or tech choices
6. Scan commit messages for decision-worthy changes (non-trivial refactors, tech choices, removals)

## Delegation

**ALWAYS** delegate the documentation updates to the `documenter` agent via the Agent tool. Do not handle documentation updates inline — even if other work (code fixes, memory saves) is also requested.

Pass to the agent:

- The resolved diff, commit range, task intent or plain-text fact as context
- The source type and any SHA(s) for Decisions log entries
- The mode: "interactive" or "unattended"
- Instruction: "Update only the docs relevant to what changed. Append Decisions entries where appropriate. Target project files (AGENTS.md, docs/*.md, README.md) — auto-memory files are not a substitute. Always review README.md for staleness; it is for human readers, so keep it concise and link to docs/ rather than duplicating them." Add "Ask the user before writing significant changes." in interactive mode, or "Unattended: don't ask, write the updates and list them in your report." in unattended mode.

## Output

With an output path, after the agent finishes, write this JSON there yourself (and nothing else outside the documentation):

```json
{
  "pr_title": "feat(sdk): learn stage writes the PR title",
  "pr_summary": "Adds a `learn` session after review that updates the docs and writes the PR text. ..."
}
```

- `pr_title`: a [Conventional Commits](https://www.conventionalcommits.org) subject for the whole change (the diff, not only the doc updates), such as `feat(worker): <what it adds>`. The scope is the area of the code the change touches (a package, module or component, as the repository's `git log` already uses), left out when the change spans several areas; never the task or issue id.
- `pr_summary`: a few sentences of markdown on what changed and why, and anything a reviewer should check, including the doc updates this run made.

Check that the file parses as JSON with exactly these two string fields; fix it if it does not.
