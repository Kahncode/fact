# Tasks

Until fact can read GitHub Issues (end of M2), all work is planned as markdown files here. Claude Code runs them first. From late M1 fact runs them, and in M2 the files that are left move to GitHub issues.

## Layout

```
tasks/
  README.md          this file
  backlog.md         ideas and problems not yet planned
  M0/01-uv-workspace.md
  M0/02-ci.md
  M1/...
```

The folder is the milestone. The file name is `<NN>-<slug>.md`, numbered in a sensible order. Dependencies, not numbers, decide what may start.

## Format

```markdown
---
id: M0-03                  # <milestone>-<NN>, unique, never reused
title: Settings and fact.yaml loading
type: feature              # feature | chore | bug
complexity: standard       # trivial | standard | complex
status: planned            # planned | ready | done
depends_on: [M0-01]        # ids that must be done before this starts
human_actions: []          # things only a person can do, before or during the task
---

## Goal
One or two sentences: what exists when this is done, and why.

## Read first
The only files and spec sections you need. Keep it short.

## Do
What to build, specific enough to start without asking.

## Out of scope
What a neighbouring task does instead.

## Acceptance criteria
- [ ] Checkable items: a command and its expected result, a test that exists and passes, or a behaviour a reviewer can verify.
```

### Fields

- **status**
  - `planned`: written, not yet approved to run.
  - `ready`: approved. It may start once every `depends_on` task is `done`.
  - `done`: merged. While bootstrapping locally, `do-task` deletes a finished task's file instead of setting this, and a dependency whose file is gone counts as done. Set to `done` in the task's own commit, so `main` always shows what is finished. While a task is running, its status lives in fact's DB, not in the file.
- **complexity** picks the model:
  - `trivial`: mechanical edits, Sonnet.
  - `standard`: normal feature work, Sonnet.
  - `complex`: design judgment or tricky concurrency, Opus.
- **human_actions**: anything an agent cannot do, such as creating a key or installing a tool. The runner stops and asks before starting a task whose human actions aren't confirmed.

## Running a task

The `do-task` skill runs a task with Claude Code. From late M1, `fact run task:<id>` does the same.

1. Check that every `depends_on` task is `done` and every `human_action` is confirmed.
2. Create a branch `fact/task-<id in lower case>`.
3. Read `CLAUDE.md`, the task, and only what its "Read first" lists.
4. Implement until every acceptance criterion holds and lint, format and tests pass.
5. Update the context (`CLAUDE.md`, `docs/spec/`, READMEs) where the change made it wrong. Set `status: done`.
6. Commit and open a PR titled `<type>(<id>): <title>` (conventional commits; `feature`→`feat`, `chore`→`chore`, `bug`→`fix`). PRs are squash-merged, so each task lands on `main` as one commit with its context update. A person merges it.

## How to split work

This is a starting heuristic; we'll adjust it with real runs.

- **One concern per task.** A task is one PR (squash-merged into one commit) that a person can review in one sitting. Don't mix concerns: "install the FastAPI framework" is one task, "add the projects endpoint" is another.
- **Size follows the concern, not a line count.** Scaffolding and mechanical changes can be large. Logic tasks are usually a few hundred lines plus tests. If the acceptance criteria don't fit on one screen, split the task.
- **Small context.** If "Read first" lists more than about six files or sections, split, or say in the task that the agent should use subagents for a wide mechanical sweep.
- **End green.** Every task leaves lint, format, tests and CI passing. Tests ship with the code.
- **Self-contained.** A task can be done from its own file, `CLAUDE.md` and its "Read first" list, without reading other tasks.
- **Explicit dependencies.** Mostly a chain, with a side branch where two tasks touch different files, so two sandboxes can work at once.
