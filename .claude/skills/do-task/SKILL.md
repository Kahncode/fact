---
name: do-task
description: Run one fact task file from tasks/ end to end, from checking its dependencies to opening the PR. Use when asked to do, run or work on a task by id (for example "do M0-03") or by path.
---

# Run a task

You are implementing one task from `tasks/`. `tasks/README.md` defines the format and the lifecycle; follow it.

1. **Find the task.** Resolve the id (such as `M0-03`) to `tasks/<milestone>/<NN>-*.md`. With no id, run `uv run python .claude/skills/do-task/next_task.py` (add `--all` to list every runnable task) and take the task it prints. Read it.
2. **Check it can start.**
   - Every id in `depends_on` must be done: its file is gone from `tasks/` (finished tasks are deleted) or it has `status: done`. If not, stop and say which are missing.
   - If `human_actions` is not empty, list them and ask the person to confirm each one. Don't start until they do.
   - If its status is already `done`, stop.
3. **No branch.** While the project is being bootstrapped we work locally: stay on main and don't create a branch.
4. **Load context.** Read `CLAUDE.md`, then only the files and spec sections under "Read first". For a wide mechanical sweep across many files, use subagents so your own context stays small.
5. **Implement** to the acceptance criteria. Write tests with the code.
6. **Check.** Run `uv run ruff check . --fix`, `uv run black .` and `uv run pytest` until all pass. Go through every acceptance criterion and run the command each one names. Check each one off only when it actually holds.
7. **Review your own diff** as a strict reviewer would: scope creep, missing tests, Windows paths, secrets, determinism in workflow code. Fix what you find.
8. **Update context.** Fix `CLAUDE.md`, `docs/spec/` and READMEs wherever this change made them wrong. If you found follow-up work, add it to `tasks/backlog.md`, not to this change.
9. **Finish.** Delete the task file (`git rm`); its history is the record. Commit directly on main, deletion included, with the message `<id>: <title>`. Don't push or open a PR for now.

If something in the task is ambiguous or wrong, stop and ask rather than guess. Write down the decision you get in the task file.
