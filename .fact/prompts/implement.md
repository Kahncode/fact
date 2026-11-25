# Implement $task_id: $task_title

You are working in a fact run workspace at `$workspace`. `WORKSPACE.md` there lists each repository's folder, branch and role. Each repository is a git worktree already on the run's branch.

## Task

$task_body

## Acceptance criteria

$acceptance_criteria

## Context

$context

## How to work

1. Read `WORKSPACE.md`, then the instructions of each repository you touch (`AGENTS.md` or `CLAUDE.md`).
2. Implement the task so every acceptance criterion holds. Write tests with the code.
3. Run each repository's own lint and tests and fix what fails.
4. Commit your work locally in each repository you changed, on the branch you are on, with a clear message: a conventional commit subject such as `feat($task_id): <what changed>` and a short body saying why.
5. Write the pull request text to `$workspace/.fact-out/$stage_id.json` as a JSON object: `title`, a conventional commit subject for the whole change such as `feat($task_id): <what it adds>`, and `summary`, a few lines of markdown saying what changed and why.

## Rules

- Never push, and never create, switch or touch any other branch, above all the default branch.
- Never rewrite history that was there before you started: no rebase, reset or force of existing commits.
- Stay within the task. Note follow-up ideas in your final message instead of doing them.
- Never commit secrets, and never write outside `$workspace`.

End with a short summary of what you changed and anything a reviewer should check.
