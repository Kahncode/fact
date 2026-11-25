---
name: next-task
description: Run one fact task file from tasks/ end to end, from checking its dependencies to the commit. Use when asked to do, run or work on a task by id (for example "do M0-03") or by path.
---

# Run a task

You are implementing one task from `tasks/`. `tasks/README.md` defines the format and the lifecycle; follow it.

1. **Find the task.** Resolve the id (such as `M0-03`) to `tasks/<milestone>/<NN>-*.md`. With no id, run `uv run python .agents/skills/next-task/next_task.py` (add `--all` to list every runnable task) and take the task it prints. Read it, then announce it: `Working on <id>: <title>`.
   If it has a `resume` field and a `## Resume` section, a previous run hit the usage limit after committing: treat that commit as unverified, re-check it against every step below, and commit only the fixes plus the task file deletion.
2. **Check it can start.**
   - Every id in `depends_on` must be done: its file is gone from `tasks/` (finished tasks are deleted) or it has `status: done`. If not, stop and say which are missing.
   - If `human_actions` is not empty, list them and ask the person to confirm each one. Don't start until they do.
   - If its status is already `done`, stop.
3. **No branch.** While the project is being bootstrapped we work locally: stay on main and don't create a branch.
4. **Load context.** Read `CLAUDE.md`, then only the files and spec sections under "Read first". For a wide mechanical sweep across many files, use subagents so your own context stays small.
5. **Implement** to the acceptance criteria. Write tests with the code. Delegate to the most relevant subagent (for example `fastapi-dev`, `backend-dev`, `test-writer`). For a complex task, split the work into independent pieces and run a team of subagents in parallel, one per piece, with clear file ownership so they don't collide. You stay responsible for integrating the result and for steps 6 to 10.
6. **Check.** Run `uv run ruff check . --fix`, `uv run black .` and `uv run pytest` until all pass. Go through every acceptance criterion and run the command each one names. Check each one off only when it actually holds.
7. **Review.** Run the `/review` skill on the diff (invoke it with the Skill tool). Also look for scope creep, missing tests, Windows paths, secrets, and determinism in workflow code. Fix what is real, then re-run step 6.
8. **Update context.** Run the `/learn` skill (invoke it with the Skill tool) so `AGENTS.md`/`CLAUDE.md`, `docs/spec/` and READMEs are fixed wherever this change made them wrong. If you found follow-up work, add it to `tasks/backlog.md`, not to this change.
9. **Delete the task file** with `git rm`; its history is the record. Stage everything that belongs to the task, deletion included.
10. **Commit.** Run the `/commit` skill (invoke it with the Skill tool) to commit directly on main.
   - **Subject**: the conventional commit `<type>(<id>): <title>`, where `<type>` is the task's `type` mapped `feature`→`feat`, `chore`→`chore`, `bug`→`fix` (for example `chore(M0-02): CI on Ubuntu and Windows`). Pass it as the skill's argument.
   - **Body**: the `/commit` skill writes it from the staged diff; don't write one here.
   - If hooks fail, the `/commit` skill's unattended rules apply. Don't push or open a PR for now.

Keep the person oriented: start each step's first message with a short line naming the task and the step, such as `M0-03 · step 6/10: running checks`. Don't repeat it for every tool call.

If something in the task is ambiguous or wrong, stop and ask rather than guess. Write down the decision you get in the task file.
