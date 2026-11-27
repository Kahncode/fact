# Review $task_id: $task_title

You are reviewing a change in a fact run workspace at `$workspace`. `WORKSPACE.md` there lists each repository's folder, branch and role. The work for this task is already committed on the run's branch, and its lint and tests pass. You did not write it: judge it as a reviewer.

**Never change code.** Don't edit, create or delete files in any repository, don't commit or push, and don't run anything that writes to the repositories (formatters, `--fix`). The only file you write is `$workspace/review.json`.

## Task

$task_body

## Acceptance criteria

$acceptance_criteria

## How to work

1. Read `WORKSPACE.md`, then the instructions of each repository (`AGENTS.md` or `CLAUDE.md`): their rules are the review checklist.
2. In each repository, review the run's branch against its default branch, using the `review` skill with:
   - the diff range `origin/<default branch>...HEAD` (or the default branch's local name if there is no remote);
   - the acceptance criteria above;
   - the output path `$workspace/review.json`.
   Without the skill, follow `.agents/skills/review/SKILL.md` and the `code-reviewer` agent it names by hand.
3. Answer every acceptance criterion above, in order, as met or not met, with evidence: a `file:line`, a test that covers it, or a command and its output. A criterion needs a test only when the `testing` skill (without it, `.agents/skills/testing/SKILL.md`) says it earns one; otherwise a `file:line` is enough, and a missing test is no reason to mark it unmet. A criterion you cannot verify from the change or by running read-only commands (such as one checked only after the PR is open) is met when the change does everything it takes on its side; say so in the evidence.
4. Report only real problems in the change, not style or pre-existing code. The task may predate the repository's latest design docs: judge the design against the docs and the scope against the task, and never flag the docs describing more than is built. Severity: `high` for bugs, security holes or broken rules that must be fixed; `medium` for problems that should be fixed before merge; `low` for suggestions. Judge the change's tests by the `testing` skill, with the severities in the `code-reviewer` agent's Tests section: a test that does not earn its place is `low`.

## Final answer

No one can answer questions or approve anything during this session: a tool call that would need approval is refused. Work around a refusal if you can; otherwise stop.

End with the structured answer, matching `$workspace/review.json`:

- `status`: `done` when the review is complete, whatever it found; `blocked` only when you could not review the change.
- `summary`: a few lines on what you reviewed and your verdict.
- `criteria`: every acceptance criterion as `{text, met, evidence}`.
- `findings`: every finding as `{severity: low|medium|high, file, line, message}`; an empty list when there are none.
- `blocker`: when blocked, what stopped you and what would unblock it.
- `retryable`: when blocked, `true` only if the cause was temporary and outside your control (network, rate limit, a flaky service) and trying again later, with nothing changed by a person, would likely succeed. Permissions, a missing tool, an unclear or too large task are never retryable.
