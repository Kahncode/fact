# Learn from $task_id: $task_title

You are working in a fact run workspace at `$workspace`. `WORKSPACE.md` there lists each repository's folder, branch and role. The work for this task is committed on the run's branch, its lint and tests pass, and it has been reviewed. Your job is to bring each repository's context in line with the change, and to write the pull request's title and summary.

## Task, for reference

$task_body

## How to work

1. Read `WORKSPACE.md`, then the instructions of each repository (`AGENTS.md` or `CLAUDE.md`).
2. In each repository that has commits on the run's branch, run the `learn` skill with the flag `unattended` and the diff range `origin/<default branch>...HEAD` (or the default branch's local name if there is no remote). Without the skill, follow `.agents/skills/learn/SKILL.md` and the `documenter` agent it names by hand.
3. Write `$workspace/pr.json` once, as the skill's Output section describes, covering the whole change across every repository. With one repository, pass the output path `$workspace/pr.json` to the skill instead.
4. Update only context the change made wrong or incomplete: `AGENTS.md` or `CLAUDE.md`, `docs/`, READMEs. Never change code, tests or task files. When nothing needs updating, change nothing.
5. Commit the doc updates locally in each repository you changed, on the branch you are on, with a message such as `docs(spec): update context`; the scope names the area of the code it touches, never the task id.

## Rules

- Never push, and never create, switch or touch any other branch, above all the default branch.
- Never rewrite existing commits: add new ones.
- Never commit secrets, and never write outside `$workspace`.

## Final answer

No one can answer questions or approve anything during this session: a tool call that would need approval is refused. Work around a refusal if you can; otherwise stop.

End with the structured answer, matching `$workspace/pr.json`:

- `status`: `done` when the context is up to date and committed (or needed no change); `blocked` when you could not finish.
- `title`: `pr_title`, the pull request title: a conventional commit subject for the whole change, such as `feat(worker): <what it adds>`; the scope names the area of the code it touches, never the task id (leave it out when the change spans several areas).
- `summary`: `pr_summary`, a few lines of markdown for the pull request: what changed, why, which docs you updated, and anything a reviewer should check.
- `blocker`: when blocked, what stopped you and what would unblock it.
- `retryable`: when blocked, `true` only if the cause was temporary and outside your control (network, rate limit, a flaky service) and trying again later, with nothing changed by a person, would likely succeed. Permissions, a missing tool, an unclear or too large task are never retryable.
