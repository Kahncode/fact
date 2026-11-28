# Resolve merge conflicts for $task_id: $task_title

You are working in a fact run workspace at `$workspace`. `WORKSPACE.md` there lists each repository's folder, branch and role. The work for this task is committed on the run's branch. The default branch moved on meanwhile, and GitHub can no longer merge the PR, so fact started merging the default branch into the run's branch (`git merge origin/<default branch>`). The merge stopped on conflicts and is still in progress.

## Conflicts

$context

## Task, for reference

$task_body

## Acceptance criteria

$acceptance_criteria

## How to work

1. In each repository listed above, run `git status` and `git diff` to see the conflicts. `git log --oneline HEAD..MERGE_HEAD` shows what the default branch brought in; `git log --oneline MERGE_HEAD..HEAD` shows this task's work.
2. Resolve every conflict so that both sides' intent survives: the default branch's changes are already merged and reviewed, so keep them, and re-apply this task's change on top. In docs and lists, usually keep both entries. Remove every conflict marker.
3. Check that the result still builds: run the repository's lint and tests where the conflicts touched code.
4. `git add` the resolved files, then conclude the merge with `git commit --no-edit`. fact checks the result, runs the checks again, and pushes it.

## Rules

- Never push, and never create, switch or touch any other branch, above all the default branch.
- Never abort the merge (`git merge --abort`), rebase, or reset: conclude the merge in progress.
- Never rewrite existing commits.
- Change only what resolving the conflicts needs.

## Final answer

No one can answer questions or approve anything during this session: a tool call that would need approval is refused. Work around a refusal if you can; otherwise stop.

End with the structured answer:

- `status`: `done` only when the merge is committed with no conflict left; `blocked` when you could not resolve it, such as two changes that cannot both hold and need a person to choose.
- `summary`: which files conflicted, and how you resolved each.
- `blocker`: when blocked, what stopped you and what would unblock it.
- `question`: when blocked because you need information or a decision only a person can give (the task is unclear, two readings change what gets built): the question to ask them, self-contained, as you would ask it on the issue. fact asks it on the task and resumes this session with the answer. Leave it out for any other blocker.
- `retryable`: when blocked, `true` only if the cause was temporary and outside your control (network, rate limit, a flaky service). A conflict that needs a person's choice is never retryable.
