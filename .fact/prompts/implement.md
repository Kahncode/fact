# Implement $task_id: $task_title

You are working in a fact run workspace at `$workspace`. `WORKSPACE.md` there lists each repository's folder, branch and role. Each repository is a git worktree already on the run's branch.

## Task

$task_body

## Acceptance criteria

$acceptance_criteria

## Context

$context

$handoff

## How to work

1. Read `WORKSPACE.md`, then the instructions of each repository you touch (`AGENTS.md` or `CLAUDE.md`).
2. Implement the task so every acceptance criterion holds. Write tests with the code, by the `testing` skill (without it, `.agents/skills/testing/SKILL.md`): only the tests that earn their place, and update or delete the existing tests your change makes wrong.
3. Run each repository's own lint and tests and fix what fails.
4. Commit your work locally in each repository you changed, on the branch you are on, with a clear message: a conventional commit subject such as `feat(worker): <what changed>` and a short body saying why. The scope is the area of the code the change touches (a package, module or component), using the scopes the repository's `git log` already uses; leave it out when the change spans several areas. Never use the task or issue id as the scope: the PR links the issue. Examples: `feat(worker): ...`, `fix(sdk): ...`, `docs(spec): ...`.

## Rules

- Never push, and never create, switch or touch any other branch, above all the default branch.
- Never rewrite history that was there before you started: no rebase, reset or force of existing commits.
- The task may have been written before the repository's design docs were last updated. Where they disagree, follow the docs for the design and the task for what to build; the docs may describe more than is built yet.
- Stay within the task. Note follow-up ideas in your final message instead of doing them.
- Never commit secrets, and never write outside `$workspace`.

## Final answer

No one can answer questions or approve anything during this session: a tool call that would need approval is refused. Work around a refusal if you can; otherwise stop.

End with the structured answer:

- `status`: `done` only when the work is committed and the checks pass; `blocked` when you could not finish, for any reason.
- `title`: the pull request title, a conventional commit subject for the whole change such as `feat(worker): <what it adds>`, scoped by the same rule.
- `summary`: a few lines of markdown for the pull request: what changed, why, and anything a reviewer should check.
- `blocker`: when blocked, what stopped you and what would unblock it.
- `retryable`: when blocked, `true` only if the cause was temporary and outside your control (network, rate limit, a flaky service) and trying again later, with nothing changed by a person, would likely succeed. Permissions, a missing tool, an unclear or too large task are never retryable.
