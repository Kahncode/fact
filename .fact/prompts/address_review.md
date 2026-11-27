# Address the review of $task_id: $task_title

You are working in a fact run workspace at `$workspace`. `WORKSPACE.md` there lists each repository's folder, branch and role. The work for this task is already committed on the run's branch; a separate review found what is below.

## Review

$context

## Task, for reference

$task_body

## Acceptance criteria

$acceptance_criteria

## How to work

1. Read each unmet criterion and finding above and check it against the code. Fix every unmet criterion and every `high` or `medium` finding; fix `low` ones when it is quick and clearly right.
2. If a finding is wrong, leave the code as it is and say why in your summary.
3. Write, update or delete tests with the fixes by the `testing` skill (without it, `.agents/skills/testing/SKILL.md`), then run each repository's own lint and tests and fix what fails.
4. Commit the fixes locally on the branch you are on, with a clear message such as `fix(worker): address review`; the scope names the area of the code it touches, never the task id.

## Rules

- Never push, and never create, switch or touch any other branch, above all the default branch.
- Never rewrite existing commits: add new ones.
- Fix only what the review raised.

## Final answer

No one can answer questions or approve anything during this session: a tool call that would need approval is refused. Work around a refusal if you can; otherwise stop.

End with the structured answer:

- `status`: `done` only when the fixes are committed and the checks pass; `blocked` when you could not address the review.
- `summary`: what you fixed, and which findings you left and why.
- `blocker`: when blocked, what stopped you and what would unblock it.
- `retryable`: when blocked, `true` only if the cause was temporary and outside your control (network, rate limit, a flaky service) and trying again later, with nothing changed by a person, would likely succeed. Permissions, a missing tool, an unclear or too large task are never retryable.
