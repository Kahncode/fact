# Fix failing checks for $task_id: $task_title

You are working in a fact run workspace at `$workspace`. `WORKSPACE.md` there lists each repository's folder, branch and role. The work for this task is already committed on the run's branch; some checks fail on it.

## Failing checks

$context

## Task, for reference

$task_body

## Acceptance criteria

$acceptance_criteria

## How to work

1. Read the failures above and reproduce them by running the same commands in the repository they came from.
2. Fix the cause in the code or the tests. Don't weaken, skip or delete a check or a test to make it pass unless the check itself is wrong, and say so if you do.
3. Re-run the checks until they pass.
4. Commit the fix locally on the branch you are on, with a clear message such as `fix($task_id): <what was wrong>`.

## Rules

- Never push, and never create, switch or touch any other branch, above all the default branch.
- Never rewrite existing commits: add new ones.
- Fix only what the failing checks need.

## Final answer

No one can answer questions or approve anything during this session: a tool call that would need approval is refused. Work around a refusal if you can; otherwise stop.

End with the structured answer:

- `status`: `done` only when the fix is committed and the checks pass; `blocked` when you could not fix them.
- `summary`: what was wrong and how you fixed it.
- `blocker`: when blocked, what stopped you and what would unblock it.
