# fact-worker

Temporal workers and activities: runners (where a run's sandbox lives) and harnesses (which coding agent runs in it).

Task queue names, activity names and activity input models (`AgentStepInput`, `ShellStepInput`, `CheckResult`, `OpenChangeSetInput`, `PostCostCommentInput`, `MarkTaskDoneInput`, `RunStartedInput`, `RunFinishedInput`) live in `fact.core.activity_contract`, so workflow code in `fact.sdk` never imports this package; the worker modules re-export the old names. `python -m fact.worker` registers `prepare_workspace`, `teardown_workspace`, `open_change_set`, `post_cost_comment` and the control activities below on the `control` queue, and `shell_step` and `agent_step` on `sandbox` (at most `concurrency.sandboxes` at once, via `max_concurrent_activities`). It also runs one workflow worker per registered project; see below.

## Workflow loading and versioning

`fact.worker.loader.load_project` pins a project to its home repository's default branch and imports its workflows (spec `docs/spec/06-workflow-authoring.md`, "How pinning is built"):

1. `LocalWorktreeRunner.sync_mirror(owner/repo)` (public) clones or fetches the bare mirror, then runs `git remote set-head origin --auto` so a renamed default branch is followed.
2. `default_branch_sha` reads `origin/HEAD`, falling back to the mirror's own `HEAD` branch.
3. `pinned_checkout` creates, or reuses when its `HEAD` matches, a detached worktree at `pinned_dir/projects/<project>/<sha>`.
4. `load_workflows` imports `.fact/workflows/*.py` in sorted order (`_`-prefixed files skipped) as `fact_workflows_<project>_<sha12>_<stem>`, through a meta path finder registered on `sys.meta_path`, so the workflow sandbox can re-import them by name. Two files mapping to the same module name are refused. It keeps the `FactWorkflow` subclasses defined in the module and decorated with `@workflow.defn`.

Any error is a `WorkflowLoadError` naming the project or file; `python -m fact.worker` logs `workflow_load_failed` and skips that project (a project with no workflows logs `no_workflows`). `factory_sha()` is the factory checkout's `HEAD`, or `unknown` when it is not a git checkout.

`fact.worker.deployments`: each loaded project gets a worker on task queue `wf-<project>` (`workflow_queue(project)` in `fact.core.activity_contract`), as version `<factory SHA[:12]>+<home SHA>` of deployment `wf-<project>`, with Worker Deployment Versioning and default behaviour `PINNED`. Once the workers start, `set_current_version` makes that version current (retried up to 10 times; startup fails if it cannot). `FactWorkflow.execute` passes the build id as `RunStartedInput.worker_version`, and `record_run_started` stores `worker_version` and `home_sha` on the run.

One process runs only the newest version: runs pinned to an older one wait after a restart on new code, so drop and restart them. Side-by-side versions (`fact upgrade`) come later. `loader.sandbox_runner()` is the sandboxed runner every workflow worker (and `test_standard_workflow.py`) uses, with `fact`, `pydantic`, `pydantic_core` and `rich` passed through.

## Runners

`fact.worker.runners` has the `Runner` protocol and `LocalWorktreeRunner` (see `docs/spec/09-runners-and-harnesses.md`). `LocalWorktreeRunner(settings, remote_url, github=None, factory_root=REPO_ROOT)`: `prepare` refuses a workspace inside `pinned_dir` or inside `factory_root` (the running factory checkout), so an agent never edits the code it runs on. With a `GitHubApp`, worktrees commit as its bot user (`fact[bot]`, from `bot_identity()`); without one, or if the lookup fails, as `commit_author_name`/`commit_author_email`. Every mirror sync keeps `core.bare = true` in the mirror's own `config.worktree` (with `extensions.worktreeConfig`), not in the shared config, so `git add`/`commit` work in the worktrees; this also repairs older mirrors. `prepare` holds a per-run-id lock (overlapping Temporal attempts take turns), first tears down any leftover folder of the same run id, then picks each repository's branch under the mirror lock with `runners/branches.py` `free_branch`: the task branch, or `<branch>-2`, `-3`, ... (up to 50; suffixed names that exist on the remote are skipped) when another worktree holds it. A held branch is never reset, so a left-behind workspace keeps its unpushed commits. The handle's `branches` and `WORKSPACE.md` carry the branch actually used. `teardown` removes the worktrees and runs the folder's `rmtree` in a thread. `fact.worker.runners.process` holds the subprocess helpers: `build_env` (a minimal environment plus what the caller passes), `spawn` (own process group; stderr merged and stdin closed unless asked otherwise) and `kill_tree`.

## Harnesses

`fact.worker.harnesses` holds the harness contract and its adapters.

- `HarnessRequest`: `prompt`, `cwd`, `model`, `allowed_tools`, `max_turns`, `permission_mode`, `output_schema` (JSON Schema for the final answer), `resume_session_id`, `env`.
- `HarnessResult`: `session_id`, `final_message`, `is_error`, `exit_code`, `stop_reason`, `num_turns`, `usage` (`fact.core.types.Usage`), `structured_output` (the final answer matching `output_schema`, or None), `permission_denials` (`PermissionDenial`: `tool_name`, `tool_input`).
- `Harness.run(request, transcript_path, on_message)` runs the agent to completion, writes every raw output line to `transcript_path` and calls `on_message` with each parsed message. Cancelling it kills the agent's process tree; the transcript written so far stays.

`ClaudeCodeHarness` runs `claude -p --output-format stream-json --verbose --permission-prompts none`, adding `--model`, `--max-turns`, `--permission-mode`, `--json-schema` (the schema as compact JSON), `--resume` and `--allowedTools` (one argument per rule, last) when set. `--permission-prompts none` keeps a headless session from ever waiting on an approval: a tool call that would prompt is denied and listed in the result's `permission_denials`. `--json-schema` contains quotes, so it needs the native `claude.exe` on Windows (see below). The prompt goes on stdin, never argv (Windows limits command-line length). When `claude` resolves to a `.cmd`/`.bat` wrapper (an npm install on Windows), cmd.exe re-parses the command line, so arguments containing `"&|<>^%!` or newlines are rejected with `HarnessError`; the native `claude.exe` has no such limit. A process that cannot start (missing binary or `cwd`) also raises `HarnessError`. An output line over 64 MiB is dropped and replaced in the transcript by a `{"type":"fact_dropped_line"}` marker. The process gets the minimal environment from `build_env` plus `request.env`, so agent credentials such as `ANTHROPIC_API_KEY` must be passed there; a logged-in Claude Code finds its credentials through `HOME`/`USERPROFILE`.

The result comes from the final `result` message:

- `stop_reason` is its `subtype`: `success`, `error_max_turns`, `error_during_execution`; `no_result` when the process ended without one (then `final_message` is the tail of stderr).
- `final_message` is `result`, or the joined `errors` when there is no `result`.
- `is_error` is the message's `is_error`, or true for a non-zero exit code.
- `usage.tokens_in` is `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`; `usage.tokens_out` is `output_tokens`; `usage.cost_usd` is `total_cost_usd`.

Tests replay recorded streams from `tests/fixtures/claude_stream_*.jsonl` through `tests/fixtures/fake_claude.py`; no network. To re-record, run Claude Code once on a trivial prompt and strip local paths and the user's tool, plugin and MCP lists from the `init` message.

### Manual check

Runs real Claude Code (needs `claude` on PATH and logged in) on a prompt in a temporary folder and prints session id, cost, tokens and stop reason:

```
uv run python -m fact.worker.harnesses "reply with OK" --model haiku
```

## Agent step

`agent_step` (`fact.worker.activities.agent`, `sandbox` queue) runs a coding agent as a reported step. `AgentStepInput`: `run_id`, `stage_id`, `attempt`, `handle`, `task`, `prompt_name`, `context`, `complexity` (default `standard`), `override` (`RouteOverride`), `home_repo` (full name in `handle.repos`; default the first repository).

1. Picks harness and model with `routing.choose` and renders `.fact/prompts/<prompt_name>.md` from the home repository's worktree, so the prompt comes from the run's branch. The output schema is `prompts.BASE_OUTPUT_SCHEMA` (`status: done|blocked`, `summary`, optional `blocker`) plus the properties of `.fact/prompts/<prompt_name>.schema.json` when present.
2. Runs the harness with `cwd` = workspace root, and `max_turns` and `permission_mode` (default `auto`) from `step_defaults.agent`; heartbeats every 30 s with the turn count (distinct assistant message ids).
3. Uploads `transcript.jsonl` as the step's `transcript_ref` (`StepContext.attach_transcript`), also on error or cancel. Each denied tool call is a `permission denied:` line in the step log.
4. Returns the step's result with `session_id`. Heartbeats carry `(progress, session_id)`; if the worker shuts down during the session, the step fails with a non-retryable `ApplicationError` of type `AGENT_INTERRUPTED` whose `details[0]` is the session id, so the workflow can resume it.
5. With `resume_session_id` set, the harness resumes that session with `prompts.render_resume(context)` instead of the stage's prompt; the output schema is still the stage prompt's.
6. `judge` turns the result into outcome, summary and outputs: the structured answer is the step's outputs (at most 64 KiB, else `failed_error`), plus `permission_denials` when there were any.

Outcomes: `passed` for `status: done`; `needs_human` for `status: blocked` (the summary is the `blocker`, followed by the denied tools); `failed_error` for an error result, a `HarnessError` or no structured answer; `stopped_by_monitor` when the stop reason is `error_max_turns`. The exit code alone never makes a step pass. An unknown harness, a missing home repository or prompt, a bad placeholder or an invalid `<prompt>.schema.json` raises a non-retryable `ApplicationError` (the step row is still finished as `failed_error`).

No `env` is passed to the harness yet, so only a Claude Code login found through `HOME`/`USERPROFILE` works; `ANTHROPIC_API_KEY` is not forwarded. `step_defaults.*.timeout_minutes` is not applied by anything yet.

## Open the PR

`open_change_set` (`fact.worker.activities.change_set`, `control` queue, stage `open_pr`, step kind `github`) pushes the run's branch and opens or updates its PR. It holds the GitHub App, so it runs only in the control zone. `OpenChangeSetInput`: `run_id`, `task`, `handle`, `branch` (the branch the workspace actually checked out, from `WorkspaceHandle.branches`), `title`, `summary`, `check_results` (`CheckResult`: `name`, `outcome`, `summary`), `run_url`. It returns `list[PRRef]` (repository, number, URL, head SHA).

1. v0 supports exactly one repository in the handle. The worktree must have `branch` checked out; a detached HEAD or another branch is refused.
2. Reads the repository (`pull_requests.repo_info`: default branch, web URL) with an installation token, then `git_push.push_branch` pushes `refs/heads/<branch>` to an explicit URL (not the worktree's `origin`) with `--no-verify`. The default branch and names starting with `-` are refused. The token reaches git only through `GIT_CONFIG_COUNT`/`KEY_n`/`VALUE_n` as an `http.<url>.extraHeader` (Basic `x-access-token`); the same environment empties `core.hooksPath` (a temporary empty folder), `credential.helper`, `core.askPass` and `http.proxy`, forces `http.sslVerify`, allows only the URL's protocol and sets `GIT_CONFIG_NOSYSTEM=1`. Output lines are redacted and go to the step log.
3. Renders the body (`pr_body.render_pr_body`, `string.Template` over `templates/pr_body.md`): summary, task id linked to the task file on the default branch, acceptance criteria as a checklist, check results, run link. Bodies over 65,000 characters are truncated.
4. Reuses the open PR from `<owner>:<branch>` into the default branch (only its body is replaced) or creates one.

A missing GitHub App, a refusal, and GitHub 401, 404 or 422 are non-retryable `ApplicationError`s; other errors retry. `python -m fact.worker` builds one `GitHubApp` per process when configured and logs `github_app_unavailable` otherwise (then this activity fails and commits use the settings identity).

## Cost comment

`post_cost_comment(PostCostCommentInput)` (`fact.worker.activities.cost_comment.CostCommentActivities(github, api_base_url, http_client)`, `control` queue, not reported as a step; M1-16) reads `GET /api/v1/runs/{run_id}` with its steps and puts the run's time and cost on each PR in `prs`. `cost_comment.render_cost_comment` writes a markdown table of every step (stage, attempt, harness / model, wall-time duration or `running`, human wait, tokens in/out, cost) with a totals row, then a table per harness and model. The comment starts with `<!-- fact:cost -->`: `upsert_comment` pages through the PR's issue comments and updates the one carrying the marker, or creates it. A missing GitHub App, an unknown run (API 4xx) and GitHub 401, 404 or 422 are non-retryable. `FactWorkflow.post_cost_comment(prs)` treats any failure as a warning: the PR is already open. `Standard` calls it right after `open_change_set`, so the comment covers every step except `open_pr`, which is still being recorded.

## Control activities

`fact.worker.activities.control.ControlActivities(api_base_url, http_client)`, `control` queue. None of them is reported as a step.

- `mark_task_done(MarkTaskDoneInput) -> bool`: in the home repository's worktree (`home_repo`, default the first), sets the task's `status: done` with `FileTaskSource.mark_done` and commits only the task file as `chore(<id>): mark done`, with hooks (an empty temporary `core.hooksPath`), fsmonitor and signing off and `GIT_CONFIG_NOSYSTEM=1`. Idempotent on retry: a task already done and committed returns False; done but uncommitted is committed. Returns False when the task source is not `files`. A missing task or home repository, or a bad `project.yaml`, is non-retryable.
- `record_run_started(RunStartedInput)` and `record_run_finished(RunFinishedInput)`: `PATCH /api/v1/runs/{id}` with `running`/`started_at` (plus `worker_version` and `home_sha` when the run is versioned), or `finished`/`outcome`/`ended_at`. A 4xx is non-retryable; other errors are left to the retry policy. `FactWorkflow` treats a failure as a warning, not a run failure.

## Workflow tests

`tests/test_loader.py`, `tests/test_deployments.py` and `tests/test_pinned_run.py` cover loading, deployment config and a pinned run that records its version (`test_pinned_run.py` starts a Temporal dev server and is skipped when `temporal` is not on PATH). `tests/test_standard_workflow.py` runs `.fact/workflows/standard.py` in a time-skipping Temporal environment with fake activities registered under the real names, and replays `tests/histories/standard_green.json`. Re-record that history after an intended workflow change:

```
FACT_RECORD_HISTORIES=1 uv run pytest worker/tests/test_standard_workflow.py
```

`tests/histories/` is excluded from detect-secrets.

## Prompts

`fact.worker.prompts.render_prompt` substitutes with `string.Template`: `$task_id`, `$task_title`, `$task_body`, `$acceptance_criteria` (one `- ` line per criterion), `$context`, `$workspace`, `$stage_id`. `$$` is a literal `$`; an unknown placeholder raises `PromptError`. Prompt names must match `[A-Za-z0-9][A-Za-z0-9_-]*`.

## Routing

`fact.worker.routing.choose(complexity, stage, override=None, routing=None) -> Route` reads `settings.routing` (`fact.yaml`). Each field, harness and model, comes separately from the first entry that sets it, in this order: `override`, `<complexity>.<stage>`, `<complexity>.*`, `*.<stage>`, else `routing.default` (`claude-code` / `sonnet`). Rule keys are validated as `<complexity|*>.<stage|*>`.
