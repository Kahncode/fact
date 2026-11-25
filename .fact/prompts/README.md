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

An agent can return structured outputs by writing a JSON object (at most 64 KiB) to `$workspace/.fact-out/$stage_id.json`; it becomes the step's `outputs`. Larger data belongs in a file the workflow stores as an artifact.

- `implement.md`: implement the task to its acceptance criteria, commit locally, and write the PR `title` and `summary` as outputs (the `Standard` workflow uses them for the PR).
- `fix_checks.md`: fix the failing checks in `$context` and commit.
