# Prompts

Prompt templates for this project's agent steps. `agent_step` renders `.fact/prompts/<prompt_name>.md` from the home repository's worktree with Python's `string.Template`:

| Placeholder | Value |
| --- | --- |
| `$task_id`, `$task_title`, `$task_body` | The task |
| `$acceptance_criteria` | The task's criteria, one `- ` line each |
| `$context` | Text the workflow passes, such as failing check output |
| `$workspace` | The workspace root, which holds `WORKSPACE.md` and every repository's worktree |
| `$stage_id` | The step's stage, such as `implement` |

Write `$$` for a literal `$`; an unknown placeholder fails the step.

Prompts are read from the run's branch, like `repo.yaml`, so a change to a prompt applies to the run that makes it.

Every agent session ends with a structured answer, a JSON object the harness validates against a schema (Claude Code's `--json-schema`); it becomes the step's `outputs` (at most 64 KiB) and decides the step's outcome:

- `status` (required): `done` makes the step `passed`; `blocked` makes it `needs_human`. A session that ends without a structured answer is `failed_error`.
- `summary` (required): markdown, the step's summary.
- `blocker`: when blocked, what stopped the agent and what would unblock it.
- `retryable`: when blocked, whether a later attempt with nothing changed by a person would likely succeed (a temporary problem). `FactWorkflow.agent` then resumes the session once, after a delay.

`<name>.schema.json` next to a prompt adds fields to that answer: an object with `properties` and an optional `required` list. Larger data belongs in a file the workflow stores as an artifact.

Sessions run headless: nobody is asked for approval, so a tool call that would prompt is refused, logged in the step's log and kept in `outputs.permission_denials`. Prompts should tell the agent to report `blocked` when that stops it.

- `implement.md` (+ `implement.schema.json`, adds `title`): implement the task to its acceptance criteria and commit locally; its `title` and `summary` are the PR text when `learn` gives none.
- `fix_checks.md`: fix the failing checks in `$context` and commit.
- `review.md` (+ `review.schema.json`, adds `criteria` and `findings`): review the change with the `review` skill against the acceptance criteria, writing `$workspace/review.json`; never changes code. `criteria` is a list of `{text, met, evidence}`, `findings` a list of `{severity: low|medium|high, file, line, message}`; `fact.sdk.review.parse_review` turns a passed step whose answer does not parse into `failed_error`.
- `address_review.md`: fix the unmet criteria and findings in `$context` (from `fact.sdk.review.review_context`) and commit.
- `fix_ci.md`: fix the PR's failing CI jobs in `$context` (from `fact.sdk.failures.ci_failure_context`: each job's name, conclusion, link and the end of its log) and commit; fact pushes it and waits for CI again. `blocked` for a failure outside the repository (runner outage, missing secret).
- `learn.md` (+ `learn.schema.json`, adds `title`): run the `learn` skill unattended on the branch's diff range with the output path `$workspace/pr.json`, commit the doc updates, and answer the PR `title` and `summary`, which `Standard` puts on the PR.
