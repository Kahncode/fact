# Fix failing CI for $task_id: $task_title

You are working in a fact run workspace at `$workspace`. `WORKSPACE.md` there lists each repository's folder, branch and role. The work for this task is committed on the run's branch and pushed to its PR, and the PR's CI failed on it. The local lint and test checks passed before the push, so look for what differs in CI: another operating system, a clean checkout, a job the local checks don't run, or a missing file.

## Failing CI jobs

Each failed job, with its link and the end of its log:

$context

## Task, for reference

$task_body

## Acceptance criteria

$acceptance_criteria

## How to work

1. Read the failures above. Find the job's definition (such as `.github/workflows/`) to see the exact commands and environment it runs.
2. Reproduce the failure locally when you can, by running the same commands in the repository it came from.
3. Fix the cause in the code, the tests or the CI configuration. Don't weaken, skip or delete a check, a test or a CI job to make it pass unless it is itself wrong, and say so if you do. A test you write or change follows the `testing` skill (without it, `.agents/skills/testing/SKILL.md`).
4. Run the repository's lint and tests again, and check they pass.
5. Commit the fix locally on the branch you are on, with a clear message such as `fix(ci): <what was wrong in CI>`; the scope names the area of the code it touches, never the task id. fact pushes it and waits for CI again.

## Rules

- Never push, and never create, switch or touch any other branch, above all the default branch.
- Never rewrite existing commits: add new ones.
- Fix only what the failing jobs need.

## Final answer

No one can answer questions or approve anything during this session: a tool call that would need approval is refused. Work around a refusal if you can; otherwise stop.

End with the structured answer:

- `status`: `done` only when the fix is committed; `blocked` when you could not fix it, such as a failure only CI can show and you cannot explain, or a CI problem outside the repository (a runner outage, a missing secret).
- `summary`: what failed in CI, why, and how you fixed it.
- `assumptions`: each assumption you made where the task or the docs did not say, one sentence each; leave it out or `[]` when you made none. They are added to the PR description.
- `blocker`: when blocked, what stopped you and what would unblock it.
- `retryable`: when blocked, `true` only if the cause was temporary and outside your control (network, rate limit, a flaky service) and trying again later, with nothing changed by a person, would likely succeed. Permissions, a missing tool, an unclear or too large task are never retryable.
