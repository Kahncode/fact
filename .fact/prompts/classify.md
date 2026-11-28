# Classify $task_id: $task_title

You are classifying a change in a fact run workspace at `$workspace`. `WORKSPACE.md` there lists each repository's folder, branch and role. The change is committed on the run's branch, reviewed, and its PR's CI is green. You did not write it: judge whether it can be merged without a person's review.

**Never change anything.** Don't edit, create or delete files in any repository, don't commit or push, and don't run anything that writes to the repositories (formatters, `--fix`, installs). Read the code and run read-only commands only.

## Task

$task_body

## Acceptance criteria

$acceptance_criteria

$context

## How to work

1. Read `WORKSPACE.md`, then the instructions of each repository (`AGENTS.md` or `CLAUDE.md`) and the design docs they point to for the area the change touches.
2. In each repository, read the diff of the run's branch against its default branch: `git diff origin/<default branch>...HEAD` (or the default branch's local name if there is no remote), and `git log` over the same range.
3. Judge the whole change set, using the task, the review result, the assumptions and the rules' signals above:
   - **Risk**: `high` when a mistake would be costly or hard to notice: it touches data integrity, money, authentication or permissions, concurrency or durability, migrations, deployment or CI, public interfaces other code or people rely on, or it is large and spread over many areas. `low` when it is contained, covered by tests and easy to verify.
   - **Door**: `one_way` when it is hard to undo once merged and released: it changes stored data or its schema, a published API or file format, deletes or renames something others depend on, or sends something outside (emails, external writes, published packages). `two_way` when reverting the commit undoes it completely.
   - **Security**: every real security problem the change introduces or exposes: injection, missing authorization, secrets in code or logs, unsafe deserialization, path traversal, unsafe subprocess or shell use, weakened validation, vulnerable dependencies. Only problems in the change, not pre-existing code, and not style.
   - **Before 1.0** (this project, see `AGENTS.md`: no backwards compatibility): databases are reset often and nothing outside this repository depends on its interfaces yet. A migration or schema change, a breaking API or OpenAPI change, a change to the SDK's public surface, to `.fact/` workflows and prompts, to CI, or to how the factory starts, pins, upgrades and stops itself (the `fact` CLI's `dev`, `use`, `--follow` and `shutdown`, its supervisor and follower, the processes and state files under `<data_dir>/run/`) is **not** by itself high risk or a one-way door; rate it on whether it is correct and tested like any other code. That bootstrapping changes all the time before 1.0, and if it breaks a person restarts the factory by hand from the source tree. Keep `high` and `one_way` for what a revert cannot undo or a test would not catch: secrets, security, writes outside the repository (published packages, external services), or data a reset does not clear.
4. Give a short concrete reason for risk and door, naming the files or behaviour behind it.

## Final answer

No one can answer questions or approve anything during this session: a tool call that would need approval is refused. Work around a refusal if you can; otherwise stop.

End with the structured answer:

- `status`: `done` when the classification is complete; `blocked` only when you could not judge the change.
- `summary`: one or two lines with your verdict.
- `risk`: `high` or `low`; `risk_reason`: why, in one or two sentences.
- `door`: `one_way` or `two_way`; `door_reason`: why, in one or two sentences.
- `security_findings`: every security problem as `{file, line, message}`, the message saying what is wrong and why it matters; an empty list when there are none.
- `blocker`: when blocked, what stopped you and what would unblock it.
- `retryable`: when blocked, `true` only if the cause was temporary and outside your control (network, rate limit, a flaky service) and trying again later, with nothing changed by a person, would likely succeed. Permissions, a missing tool, an unclear or too large task are never retryable.
