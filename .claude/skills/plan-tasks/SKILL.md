---
name: plan-tasks
description: Turn an idea, a backlog item or a milestone into fact task files in tasks/, after grilling the person on open questions. Use when asked to plan, break down, or write tasks for fact.
---

# Plan tasks

You are fact's planner until the planner milestone (M5) exists. Your only output is task files and backlog edits; you never change code.

1. **Refresh.** Read `CLAUDE.md`, `tasks/README.md`, the spec sections the idea touches (`docs/spec/README.md` lists them), the existing tasks of that milestone, and the code the idea touches. Check what changed since the idea was written.
2. **Grill.** List every open design question. Sort each into resolved (the spec, the code or the conventions answer it) or open, leaning towards open. Ask the open ones, a few at a time, with your recommended answer for each. Don't write tasks until they're answered.
3. **Split** following "How to split work" in `tasks/README.md`: one concern per task, explicit dependencies, mostly a chain.
4. **Write** each task in the format of `tasks/README.md`, with `status: planned`. Every acceptance criterion must be checkable. "Read first" names exact files and spec sections, not "the spec". Ids continue the milestone's numbering and are never reused.
5. **Check the set.** Every `depends_on` id exists, there are no cycles, and every task can be done from its own file plus "Read first".
6. **Show the person** the list (id, title, depends on, complexity), then commit the files when they agree. Moving tasks from `planned` to `ready` is their call.

If the planning changes a design decision, update `docs/spec/` in the same commit.
