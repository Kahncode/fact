# fact-worker

Temporal workers and activities: runners (where a run's sandbox lives) and harnesses (which coding agent runs in it).

Task queue names, activity names and activity input models (`AgentStepInput`, `ShellStepInput`, `CheckResult`, `CriterionResult`, `OpenChangeSetInput`, `PostCostCommentInput`, `WaitForCIInput`, `MarkTaskDoneInput`, `RunStartedInput`, `RunFinishedInput`, `GateOpenedInput`, `GateAnsweredInput`, `NotifyInput`, `WaitForPRClosedInput`, `HandoffInput`, `HandoffStep`, and the `PRState` literal) live in `fact.core.activity_contract`, so workflow code in `fact.sdk` never imports this package; the worker modules re-export the old names. `python -m fact.worker` registers `prepare_workspace`, `teardown_workspace`, `open_change_set`, `post_cost_comment`, `wait_for_ci`, `wait_for_pr_closed`, `notify`, `write_handoff`, the gate activities and the control activities below on the `control` queue, and `shell_step` and `agent_step` on `sandbox` (at most `concurrency.sandboxes` at once, via `max_concurrent_activities`). The `control` worker also runs the `SchedulerTick` workflow and the scheduler activities, and startup creates or updates the `fact-scheduler` Schedule (see Scheduler below). It also runs one workflow worker per registered project; see below.

## Workflow loading and versioning

`fact.worker.loader.load_project` pins a project to its home repository's default branch and imports its workflows (spec `docs/spec/06-workflow-authoring.md`, "How pinning is built"):

1. `LocalWorktreeRunner.sync_mirror(owner/repo)` (public) clones or fetches the bare mirror, then runs `git remote set-head origin --auto` so a renamed default branch is followed.
2. `default_branch_sha` reads `origin/HEAD`, falling back to the mirror's own `HEAD` branch.
3. `pinned_checkout` creates, or reuses when its `HEAD` matches, a detached worktree at `pinned_dir/projects/<project>/<sha>`.
4. `load_workflows` imports `.fact/workflows/*.py` in sorted order (`_`-prefixed files skipped) as `fact_workflows_<project>_<sha12>_<stem>`, through a meta path finder registered on `sys.meta_path`, so the workflow sandbox can re-import them by name. Two files mapping to the same module name are refused. It keeps the `FactWorkflow` subclasses defined in the module and decorated with `@workflow.defn`.

Any error is a `WorkflowLoadError` naming the project or file; `python -m fact.worker` logs `workflow_load_failed` and skips that project (a project with no workflows logs `no_workflows`). `factory_sha()` is the factory checkout's `HEAD`, or `unknown` when it is not a git checkout.

`fact.worker.deployments`: each loaded project gets a worker on task queue `wf-<project>` (`workflow_queue(project)` in `fact.core.activity_contract`), as version `<factory SHA[:12]>+<home SHA>` of deployment `wf-<project>`, with Worker Deployment Versioning and default behaviour `PINNED`. Once the workers start, `set_current_version` makes that version current (retried up to 10 times; startup fails if it cannot). `FactWorkflow.execute` passes the build id as `RunStartedInput.worker_version`, and `record_run_started` stores `worker_version` and `home_sha` on the run.

One process runs only the newest version: runs pinned to an older one wait after a restart on new code. `fact drop` is handled by the run's own version, so drop runs (and let them show `dropped`) before restarting; a run whose version no worker runs anymore, or started before M2-06, never processes the drop: delete its `<data_dir>/workspaces/<run_id>` by hand, or the next run of the task gets a suffixed branch and a new PR. This includes a run waiting for its PR to merge (`wait_for_review`): restarted on new code, it stalls after the merge. After merging, wait until the run shows `merged` (within about 60 s) before restarting. Side-by-side versions (`fact upgrade`) come later. `loader.sandbox_runner()` is the sandboxed runner every workflow worker (and the `Standard` tests, through `tests/standard_harness.py`) uses, with `fact`, `pydantic`, `pydantic_core` and `rich` passed through.

## Runners

`fact.worker.runners` has the `Runner` protocol and `LocalWorktreeRunner` (see `docs/spec/09-runners-and-harnesses.md`). `LocalWorktreeRunner(settings, remote_url, github=None, factory_root=REPO_ROOT)`: `prepare` refuses a workspace inside `pinned_dir` or inside `factory_root` (the running factory checkout), so an agent never edits the code it runs on. With a `GitHubApp`, worktrees commit as its bot user (`fact[bot]`, from `bot_identity()`); without one, or if the lookup fails, as `commit_author_name`/`commit_author_email`. Every mirror sync keeps `core.bare = true` in the mirror's own `config.worktree` (with `extensions.worktreeConfig`), not in the shared config, so `git add`/`commit` work in the worktrees; this also repairs older mirrors. `prepare` holds a per-run-id lock (overlapping Temporal attempts take turns), first tears down any leftover folder of the same run id, then picks each repository's branch under the mirror lock with `runners/branches.py` `free_branch`: the task branch, or `<branch>-2`, `-3`, ... (up to 50; suffixed names that exist on the remote are skipped) when another worktree holds it. A held branch is never reset, so a left-behind workspace keeps its unpushed commits. The handle's `branches` and `WORKSPACE.md` carry the branch actually used. `teardown` removes the worktrees and runs the folder's `rmtree` in a thread. `fact.worker.runners.process` holds the subprocess helpers: `build_env` (the worker's environment plus what the caller passes), `spawn` (own process group; stderr merged and stdin closed unless asked otherwise) and `kill_tree`.

## Harnesses

`fact.worker.harnesses` holds the harness contract and its adapters.

- `HarnessRequest`: `prompt`, `cwd`, `model`, `allowed_tools`, `max_turns`, `permission_mode`, `output_schema` (JSON Schema for the final answer), `resume_session_id`, `env`.
- `HarnessResult`: `session_id`, `final_message`, `is_error`, `exit_code`, `stop_reason`, `num_turns`, `usage` (`fact.core.types.Usage`), `structured_output` (the final answer matching `output_schema`, or None), `permission_denials` (`PermissionDenial`: `tool_name`, `tool_input`).
- `Harness.run(request, transcript_path, on_message)` runs the agent to completion, writes every raw output line to `transcript_path` and calls `on_message` with each parsed message. Cancelling it kills the agent's process tree; the transcript written so far stays.

`ClaudeCodeHarness` runs `claude -p --output-format stream-json --verbose --permission-prompts none`, adding `--model`, `--max-turns`, `--permission-mode`, `--json-schema` (the schema as compact JSON), `--resume` and `--allowedTools` (one argument per rule, last) when set. `--permission-prompts none` keeps a headless session from ever waiting on an approval: a tool call that would prompt is denied and listed in the result's `permission_denials`. `--json-schema` contains quotes, so it needs the native `claude.exe` on Windows (see below). The prompt goes on stdin, never argv (Windows limits command-line length). When `claude` resolves to a `.cmd`/`.bat` wrapper (an npm install on Windows), cmd.exe re-parses the command line, so arguments containing `"&|<>^%!` or newlines are rejected with `HarnessError`; the native `claude.exe` has no such limit. A process that cannot start (missing binary or `cwd`) also raises `HarnessError`. An output line over 64 MiB is dropped and replaced in the transcript by a `{"type":"fact_dropped_line"}` marker. The process gets the worker's environment (`build_env`) plus `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` (no auto-memory carried between runs or projects; `request.env` can override it) plus `request.env`. A logged-in Claude Code finds its credentials through `HOME`/`USERPROFILE`; an `ANTHROPIC_API_KEY` in the environment `fact dev` starts from wins over that login and bills the API instead of the subscription, so leave it unset there.

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

`agent_step` (`fact.worker.activities.agent`, `sandbox` queue) runs a coding agent as a reported step. `AgentStepInput`: `run_id`, `stage_id`, `attempt`, `handle`, `task`, `prompt_name`, `context`, `complexity` (default `standard`), `override` (`RouteOverride`), `home_repo` (full name in `handle.repos`; default the first repository), `handoff_ref` (artifact key of a dropped run's handoff note, from `RunInput.handoff_ref`). With `handoff_ref`, the note is read through the API (`StepReporter.download_text`) and rendered as `$handoff`; a missing or unreadable note leaves `$handoff` empty, and the step log says which. The workflow runs it with `WAIT_CANCELLATION_COMPLETED`, so on a drop the cancellation reaches the activity at its next heartbeat and the workflow waits until the session has stopped before tearing the workspace down.

1. Picks harness and model with `routing.choose` and renders `.fact/prompts/<prompt_name>.md` from the home repository's worktree, so the prompt comes from the run's branch. The output schema is `prompts.BASE_OUTPUT_SCHEMA` (`status: done|blocked`, `summary`, optional `blocker`) plus the properties of `.fact/prompts/<prompt_name>.schema.json` when present.
2. Runs the harness with `cwd` = workspace root, and `max_turns` and `permission_mode` (default `auto`) from `step_defaults.agent`; heartbeats every 30 s with the turn count (distinct assistant message ids).
3. Uploads `transcript.jsonl` as the step's `transcript_ref` (`StepContext.attach_transcript`), also on error or cancel. Each denied tool call is a `permission denied:` line in the step log.
4. Returns the step's result with `session_id`. Heartbeats carry `(progress, session_id)`; if the worker shuts down during the session, the step fails with a non-retryable `ApplicationError` of type `AGENT_INTERRUPTED` whose `details[0]` is the session id, so the workflow can resume it.
5. With `resume_session_id` set, the harness resumes that session with `prompts.render_resume(context)` instead of the stage's prompt; the output schema is still the stage prompt's.
6. `judge` turns the result into outcome, summary and outputs: the structured answer is the step's outputs (at most 64 KiB, else `failed_error`), plus `permission_denials` when there were any.

Outcomes: `passed` for `status: done`; `needs_human` for `status: blocked` (the summary is the `blocker`, followed by the denied tools); `failed_error` for an error result, a `HarnessError` or no structured answer; `stopped_by_monitor` when the stop reason is `error_max_turns`. The exit code alone never makes a step pass. An unknown harness, a missing home repository or prompt, a bad placeholder or an invalid `<prompt>.schema.json` raises a non-retryable `ApplicationError` (the step row is still finished as `failed_error`).

No `env` is passed to the harness yet, so only a Claude Code login found through `HOME`/`USERPROFILE` works; `ANTHROPIC_API_KEY` is not forwarded. `step_defaults.*.timeout_minutes` is not applied by anything yet.

## Open the PR

`open_change_set` (`fact.worker.activities.change_set`, `control` queue, stage `open_pr`, step kind `github`) pushes the run's branch and opens or updates its PR. It holds the GitHub App, so it runs only in the control zone. `OpenChangeSetInput`: `run_id`, `task`, `handle`, `branch` (the branch the workspace actually checked out, from `WorkspaceHandle.branches`), `title`, `summary`, `check_results` (`CheckResult`: `name`, `outcome`, `summary`), `criteria` (`CriterionResult`: `text`, `met`, `evidence`, from the review), `run_url`, `attempt` (of stage `open_pr`, default 1; `FactWorkflow.push_change_set` pushes again after a CI fix as the next attempt). It returns `list[PRRef]` (repository, number, URL, head SHA).

1. v0 supports exactly one repository in the handle. The worktree must have `branch` checked out; a detached HEAD or another branch is refused.
2. Reads the repository (`pull_requests.repo_info`: default branch, web URL) with an installation token, then `git_push.push_branch` pushes `refs/heads/<branch>` to an explicit URL (not the worktree's `origin`), with the user's git config and hooks. The default branch and names starting with `-` are refused. The token reaches git only through `GIT_CONFIG_COUNT`/`KEY_n`/`VALUE_n` as an `http.<url>.extraHeader` (Basic `x-access-token`); the same environment empties `credential.helper`, `core.askPass` and `GIT_ASKPASS`, so a rejected token fails the push instead of prompting or falling back to the user's credentials. Output lines are redacted and go to the step log.
3. Renders the body (`pr_body.render_pr_body`, `string.Template` over `templates/pr_body.md`): summary, task id linked to the task file on the default branch, acceptance criteria as the review's table (criterion, met or not, evidence), or as a checklist when there was no review, check results, run link. Bodies over 65,000 characters are truncated.
4. Reuses the open PR from `<owner>:<branch>` into the default branch (only its body is replaced) or creates one.

A missing GitHub App, a refusal, and GitHub 401, 404 or 422 are non-retryable `ApplicationError`s; other errors retry. `python -m fact.worker` builds one `GitHubApp` per process when configured and logs `github_app_unavailable` otherwise (then this activity fails and commits use the settings identity).

## Cost comment

`post_cost_comment(PostCostCommentInput)` (`fact.worker.activities.cost_comment.CostCommentActivities(github, api_base_url, http_client)`, `control` queue, not reported as a step; M1-16) reads `GET /api/v1/runs/{run_id}` with its steps and puts the run's time and cost on each PR in `prs`. `cost_comment.render_cost_comment` writes a markdown table of every step (stage, attempt, harness / model, wall-time duration or `running`, human wait, tokens in/out, cost) with a totals row, then a table per harness and model. The comment starts with `<!-- fact:cost -->`: `upsert_comment` pages through the PR's issue comments and updates the one carrying the marker, or creates it. A missing GitHub App, an unknown run (API 4xx) and GitHub 401, 404 or 422 are non-retryable. `FactWorkflow.post_cost_comment(prs)` treats any failure as a warning: the PR is already open. `Standard` calls it right after `open_change_set` (that comment covers every step except `open_pr`, which is still being recorded), again once CI is green (or after a rejected CI gate), so the reviewer sees the CI wait and fixes, and last after the wait for review, so the final comment includes it.

## Wait for CI

`wait_for_ci(WaitForCIInput) -> StepResult` (`fact.worker.activities.ci.CIActivities(github, reporter, poll_interval_s=30)`, `control` queue, stage `wait_for_ci` by default, step kind `github`; M1-19). `WaitForCIInput`: `run_id`, `stage_id`, `attempt`, `pr` (`PRRef`), `timeout_s` (3600), `no_checks_grace_s` (300). GitHub reads are in `fact.worker.ci_checks`.

1. Polls the PR head SHA every 30 s: check runs (latest per name, GitHub's default filter, up to 10 pages of 100) and commit statuses (the combined status), each normalized to a `CICheck`. A check run of the `github-actions` app carries its job id. `success`, `neutral` and `skipped` pass; any other conclusion fails.
2. Heartbeats with its wall-clock start, so a retried attempt keeps the same deadline (the workflow sets the activity's start-to-close to 65 min and `INFRA_RETRY`).
3. Finishes when every check is complete on two polls in a row, so suites that start late (`needs:`, other workflows) get time. No check or status at all after `no_checks_grace_s` is `passed` ("no CI checks"). A transient GitHub or network error only skips a poll; 401, 404 and 422 are non-retryable, as is a missing GitHub App.
4. Outcome `passed`, or `failed_checks` with `outputs` `head_sha`, `checks`, `pending` and `failures`: per failed check `name`, `conclusion`, `url`, `log_tail` (last 4 KB) and, for a GitHub Actions job (the first 10), `log_ref`. The job log is downloaded (GitHub redirects to a signed URL), stripped of the BOM and per-line timestamps, and stored whole as the step artifact `runs/<run>/<stage>/<attempt>/job-<id>.log`; an unreachable or expired log falls back to the check's output text. Other checks use their output text or the status description. The summary (2 KB cap) lists the failed names plus as much of the tails as fits.
5. At `timeout_s`, failures already known are still reported as above; with none, `failed_checks` with `outputs.timed_out = true` and `pending` (check names).

`FactWorkflow.until_ci_green` (in `fact.sdk.pull_requests`) feeds `outputs.failures` to the fix session through `fact.sdk.failures.ci_failure_context`; it does not start a fix after a timeout.

## Wait for merge or close

`wait_for_pr_closed(WaitForPRClosedInput) -> list[PRState]` (`fact.worker.activities.pr_state.PRStateActivities(github, poll_interval_s=60)`, `control` queue; M2-05). `WaitForPRClosedInput`: `run_id`, `prs`. It polls `GET /repos/{repo}/pulls/{number}` for each PR still open every 60 s and heartbeats between polls; once none is open it returns `merged` or `closed` per PR, in order. A transient GitHub or network error only skips a poll; 401, 404 and 422, and a missing GitHub App, are non-retryable. It reports no step: `FactWorkflow.wait_for_pr_closed` (in `fact.sdk.pull_requests`) records the wait as a gate-like step (stage `wait_for_review`, kind `gate`) through the gate activities, and runs this activity for up to 30 days with `LONG_WAIT_RETRY`, so worker restarts do not use up its attempts.

## Control activities

`fact.worker.activities.control.ControlActivities(api_base_url, http_client)`, `control` queue. None of them is reported as a step.

- `mark_task_done(MarkTaskDoneInput) -> bool`: in the home repository's worktree (`home_repo`, default the first), deletes the task's file (`Task.path`, read from the default branch when the run started) and commits only that deletion as `chore(<id>): mark done`. Git runs with the user's config and the repository's hooks, with literal pathspecs and signing off; the environment is the minimal one `run_git` builds. A commit refused by a hook is non-retryable. Idempotent: a file already deleted on the branch (by an earlier attempt or by the agent) returns False; deleted but uncommitted is committed. Returns False when the task source is not `files`. A task without a path, a missing home repository or a bad `project.yaml` is non-retryable.
- `record_run_started(RunStartedInput)` and `record_run_finished(RunFinishedInput)`: `PATCH /api/v1/runs/{id}` with `running`/`started_at` (plus `worker_version` and `home_sha` when the run is versioned), or `finished`/`outcome`/`ended_at`. A 4xx is non-retryable; other errors are left to the retry policy. `FactWorkflow` treats a failure as a warning, not a run failure.

## Gate activities

`fact.worker.activities.gates.GateActivities(api_base_url, http_client)`, `control` queue (M2-01). `FactWorkflow.gate` (in `fact.sdk.human`) calls them around its wait, best effort: a failure only logs a warning, and the gate still holds the run.

- `record_gate_opened(GateOpenedInput) -> str`: creates the gate's step (`POST /api/v1/runs/{id}/steps`, kind `gate`, the gate's stage and attempt, `queued_at` and `started_at` = `opened_at`, the workflow time), PATCHes it to `waiting` with `outputs` `gate_id`, `kind`, `details`, PATCHes the run to `waiting`, and returns the step id. On a 409 (a retry already created it) it finds the step by stage and attempt in `GET /runs/{id}`, so a retry is idempotent.
- `record_gate_answered(GateAnsweredInput)`: PATCHes the step `finished` with `outcome` (`passed` for approve, `needs_human` for reject), `started_at`/`ended_at` (opened and answered, in workflow time), `wait_seconds` (the whole wall time, so the step's machine time is 0 and the run's `human_wait_seconds` gets it) and `outputs` plus `decision` and `note`; then the run back to `running`. With `step_id` None (the open was not recorded) it creates the step first.

A 4xx is a non-retryable `ApplicationError`; a 5xx or network error is left to `INFRA_RETRY`. Answering a gate (the `answer_gate` update) is done by a client: `POST /api/v1/runs/{id}/gates/{gate_id}` or `fact gate` (M2-02).

## Notify activity

`fact.worker.activities.notify.NotifyActivities(api_base_url, http_client)`, `control` queue (M2-03). Not reported as a step.

- `notify(NotifyInput) -> int`: `POST /api/v1/notifications` with the run id, `kind`, `title`, `body`, `link` and `dedupe_key`, and returns the notification's id. The API keeps one notification per `dedupe_key` (200 with the existing one on a repeat), so a retry never duplicates it. A 4xx is a non-retryable `ApplicationError`; a 5xx or network error is left to the retry policy.

`FactWorkflow.notify` (in `fact.sdk.human`) calls it best effort, with dedupe key `<run_id>:<key or kind>`: a failure only logs a warning. `FactWorkflow.gate` notifies `gate_waiting` once per gate; `Standard` calls `notify_prs_ready` once CI is green, before waiting for review, and `notify_run_end` when the run ends. `tests/test_standard_notify.py` covers `Standard`'s notifications against the fake activities.

## Handoff note

`fact.worker.activities.handoff.HandoffActivities(api_base_url, http_client)`, `control` queue (M2-06). Not reported as a step.

- `write_handoff(HandoffInput) -> str`: renders the note with `fact.worker.handoff.render_handoff` (heading `## Handoff from a dropped run`, the reason, the last stage and attempt reached, the open PR links, then each agent step's stage, attempt, outcome and summary) and stores it with `PUT /api/v1/artifacts/<key>` at `fact.core.artifacts.handoff_key(run_id)` (`runs/<run_id>/handoff.md`); returns the key. The key is the run's, so a retry overwrites the same note. A 4xx is non-retryable; a 5xx or network error is left to the retry policy.

`FactWorkflow.drop` (in `fact.sdk.drop`) calls it best effort when a person drops the run, before tearing the workspace down and recording `dropped`. `POST /api/v1/runs/start` finds the newest note for the task (`fact.api.run_start.latest_handoff`) and passes its key as `RunInput.handoff_ref`; `agent_step` reads it back into `$handoff`.

## Scheduler

The scheduler starts ready file tasks by itself (M2-07). Its contract (names, models, `plan_starts`) is `fact.core.scheduler`.

- `fact.worker.scheduler.ensure_schedule(client, settings)` runs at every worker start, after the project versions are made current. With no `fact-scheduler` Schedule it creates one: every `concurrency.scheduler_interval_seconds` (30), overlap `SKIP` so a slow tick never stacks, action `SchedulerTick` on `control` (id `fact-scheduler-tick` plus the scheduled time, 10 min execution timeout), paused unless `concurrency.scheduler_enabled`. On an existing Schedule it changes only the interval, when `fact.yaml` changed it; it never pauses or unpauses it (`fact scheduler on|off` or the Temporal UI do). A change to the action or policy in code reaches an existing Schedule only once it is deleted (in the Temporal UI or with `temporal schedule delete --schedule-id fact-scheduler`) and the worker restarts.
- `SchedulerTick` calls `find_ready_tasks`, orders and caps the result with `plan_starts`, then calls `start_scheduled_run` once per task. Each start is tried once (`maximum_attempts=1`: a start whose answer was lost may have started the run); a failed or refused start is logged and listed in `TickResult.failed`, and the tick goes on.
- `fact.worker.activities.scheduler.SchedulerActivities(api_base_url, http_client)`, both through the API, so scheduled starts take the same path as `fact run`: `find_ready_tasks` is `GET /api/v1/scheduler/ready`; `start_scheduled_run` is `POST /api/v1/runs/start` with `only_if_never_run: true`, returning the workflow id, or `""` when the API refuses with a 4xx (the task changed or already has a run); a 5xx raises.

A task that has had any run is never offered again; restart it with `fact run` (see `docs/decisions-2.md`). Tests: `tests/test_scheduler_tick.py` (order and caps, the start activity's answers) and `tests/test_scheduler.py` (ticks against the real API start each task once within the cap; the Schedule is created once and follows the interval, on `WorkflowEnvironment.start_local()` because the time-skipping server has no Schedules).

## Workflow tests

`tests/test_loader.py`, `tests/test_deployments.py` and `tests/test_pinned_run.py` cover loading, deployment config and a pinned run that records its version (`test_pinned_run.py` starts a Temporal dev server and is skipped when `temporal` is not on PATH). `tests/test_standard_workflow.py` runs `.fact/workflows/standard.py` in a time-skipping Temporal environment with fake activities registered under the real names, and replays the green run's history to check the workflow is deterministic; `tests/test_standard_review.py`, `tests/test_standard_learn.py` and `tests/test_standard_ci.py` cover the review, learn and CI fix stages; `tests/test_standard_gates.py` covers the `needs_human` gates (approve and a replay of that history, reject with the status query, unknown and closed gate ids, steering, a several-day wait with no activity or timer scheduled) through `tests/gate_harness.py` (`open_gate` polls the `status` query, `answer` sends the `answer_gate` update), and `tests/test_gate_activities.py` the gate activities against a mocked API; `FakeActivities.gate_decisions` answers each gate from inside the fake `record_gate_opened` (in order, then reject; `None` lets the test answer), because the time-skipping server, when the workflow waits with nothing scheduled while the test awaits its result, skips straight to the workflow timeout; `tests/test_wait_for_ci.py` runs the `wait_for_ci` activity against mocked GitHub responses (pending then success, failure with a job log, expired log, timeout, no checks, transient error, App not installed). `tests/test_standard_merge.py` covers the wait for review (ends `merged` or `closed` and tears down, `prs_ready` and the cost comment before the wait, a failed wait that ends its step and fails the run), and `tests/test_wait_for_pr_closed.py` runs `wait_for_pr_closed` against mocked GitHub responses (open then merged with heartbeats, several PRs, transient error, PR not found, no App). `tests/test_standard_drop.py` drops `Standard` at a gate, during the wait for review (the PR is kept; the history replays) and during an agent session (teardown only after the session stopped), and checks a restart passes `handoff_ref` to every agent session; `tests/test_handoff.py` covers rendering, `write_handoff` through the API, the note reaching `$handoff` and a missing note left out; `tests/test_change_set.py::test_restart_after_a_drop_reuses_the_branch_and_the_pr` checks with real git and mocked GitHub that the next run gets the same branch, starts at the dropped run's head and reuses the PR. The shared fakes and helpers (`FakeActivities`, `run_standard`, `load_standard`) live in `tests/standard_harness.py`; `FakeActivities.pr_state` sets how each PR ends, and `FakeActivities.gates(name)` returns gate activity calls without the review wait; `FakeActivities.write_handoff` records the note, and the test workers use `OPTIONS` (a 50 ms `max_heartbeat_throttle_interval`) so a cancellation reaches activities quickly. The shared run input and scripted answers (`REPO`, `BRANCH`, `PR`, `RUN_URL`, `MET`, `UNMET`, `LOW`, `HIGH`, `CLEAN_REVIEW`, `LEARNED`, `run_input`) live in `tests/standard_inputs.py`. Both are imported by bare name (`standard_harness`, `standard_inputs`) because `tests/conftest.py` puts `worker/tests` on `sys.path`. No histories are committed: runs in flight stay on the worker build that started them (see versioning above), so a workflow change never has to replay an older history.

## Prompts

`fact.worker.prompts.render_prompt` substitutes with `string.Template`: `$task_id`, `$task_title`, `$task_body`, `$acceptance_criteria` (one `- ` line per criterion), `$context`, `$workspace`, `$stage_id`, `$handoff` (a dropped run's handoff note, else empty). `$$` is a literal `$`; an unknown placeholder raises `PromptError`. Prompt names must match `[A-Za-z0-9][A-Za-z0-9_-]*`.

## Routing

`fact.worker.routing.choose(complexity, stage, override=None, routing=None) -> Route` reads `settings.routing` (`fact.yaml`). Each field, harness and model, comes separately from the first entry that sets it, in this order: `override`, `<complexity>.<stage>`, `<complexity>.*`, `*.<stage>`, else `routing.default` (`claude-code` / `sonnet`). Rule keys are validated as `<complexity|*>.<stage|*>`.
