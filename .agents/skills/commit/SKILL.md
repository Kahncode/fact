---
name: commit
description: Smart commit — stages nothing extra, commits with a conventional message, stops if pre-commit hooks fail
scope: project
allowed-tools: Bash(git add:*), Bash(git status:*), Bash(git commit:*), Bash(git diff:*), Bash(git branch:*), Bash(git log:*)
argument-hint: [optional commit message override]
---

# Smart Commit

## Context

- Current status: !`git status --short`
- Staged diff summary: !`git diff --cached --stat`
- Current branch: !`git branch --show-current`
- Recent commits: !`git log --oneline -5`

## Step 0 — Staged Files Check

Run `git diff --cached --name-only` to check what is staged.

- **If nothing is staged:** Tell the user: "Nothing is staged. Stage the files you want to commit with `git add <files>` and re-run `/commit`." Then **stop immediately** — do not proceed.
- **If files are staged:** Commit ONLY those files. **Never run `git add` on any file that was not already staged before `/commit` was invoked** — even if other dirty or untracked files appear in context. Unstaged files are the user's responsibility to stage explicitly.

## Step 1 — Safety Checks

Before anything else:

1. If `.gitignore` is staged, verify `.env` or other secret files are is in `.gitignore`
2. Verify no sensitive files are staged (`.env`, `secrets.json`, private keys) — **abort and warn if found**
3. If on `main` or `master`: warn the user and suggest creating a feature branch first

## Step 2 — Commit

### Message rules

1. Use **conventional commit** format: `type(scope): description`
   - Types: feat, fix, docs, style, refactor, test, chore, perf
   - Scope: optional, identifies the area changed
2. Subject: concise, max 72 characters, imperative mood
3. Body: for anything beyond a trivial one-file change, add a blank line and then 3 to 8 short bullets written from `git diff --cached`: what was added or changed and where (modules, endpoints, commands), why when it isn't obvious, and any decision or deviation from the plan. Don't restate the subject or list every file. One bullet per line, no hard wrapping. Skip the body only for trivial changes.
4. If changes span multiple unrelated concerns: suggest splitting into multiple commits
5. NEVER commit `.env` or secret files — abort and warn if found staged

### If message provided

Use `$ARGUMENTS` as the **subject** (still apply conventional format if it fits). A provided subject does not remove the body: still write the body from the staged diff, unless `$ARGUMENTS` already contains one.

### If no message provided

Generate an appropriate message based on **only the staged changes** (use `git diff --cached` to analyze what will be committed).

### Pre-commit hook failures

In an unattended session (the system prompt says nobody can answer), fix the failures, stage the files the hooks changed and commit again instead of stopping. Otherwise, if the commit attempt triggers pre-commit hooks that fail:

1. Diagnose each failure and fix the affected files
2. **Do NOT stage any files modified as a result of fixing pre-commit errors**
3. **Stop here** — report to the user: which hooks failed, what was fixed, and which files were changed
4. Ask the user to review the fixes and stage them manually before re-running `/commit`

### GitHub MCP (if available)

If the GitHub MCP server is connected:

- After a successful commit, offer: "Create a PR for this branch?"
- If yes: use the MCP to create the PR with a generated title and description
- If the branch has no upstream yet, push it first
