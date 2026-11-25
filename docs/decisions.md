# Decisions

Implementation decisions that a future developer would ask "why" about. Design-level decisions are in `docs/spec/16-decisions.md`. Entries are append-only.

## Decisions

### [2026-11-24 12:00] Request logging follows the py-fastapi-template, as pure ASGI middleware

**What:** `RequestIdMiddleware` (`@api/src/fact/api/middleware.py`) is a pure ASGI class, not `BaseHTTPMiddleware`. It validates or generates `X-Request-ID`, binds `request_id`, and logs one `request_completed` line per request (not `/health`) with JSON bodies up to 8 KiB; secret-looking keys, headers and query parameters are masked (`@api/src/fact/api/body_capture.py`). `unhandled_exception_handler` passes `request_id` explicitly and adds `detail` to the 500 only in `local` and `test`.
**Why:** Matches the template (spec `13-storage-and-stack.md`). Pure ASGI keeps contextvars bound around the whole app and lets the middleware wrap the response stream to capture bodies. The exception handler runs in `ServerErrorMiddleware`, outside this middleware, so the contextvar is already unbound there and the id must be passed by hand.

### [2026-11-24 12:00] Per-environment log level and Cloud Logging format live in Settings

**What:** `Settings.log_level` (`FACT_LOG_LEVEL`) overrides a default of DEBUG in `local`/`dev` and INFO in `test`/`prod`; `Settings.log_gcp` switches JSON output to Cloud Logging fields. `create_app` calls `configure_logging`; `python -m fact.api` runs uvicorn with `log_config=None` and `access_log=False` so one structlog pipeline owns all output.
**Why:** One place configures logging for every entry point; uvicorn's own access log would duplicate `request_completed`.

### [2026-11-24 12:00] Every route must be registered in TESTED_ROUTES

**What:** `@api/tests/test_routes.py` fails if an app route is not in `TESTED_ROUTES` (path, method, test names), or if a listed test does not exist. `None` marks a route deliberately untested.
**Why:** A new route cannot ship without a conscious test decision.

### [2026-11-24 18:00] `fact dev` stops children one at a time, last started first

**What:** The supervisor (`@cli/src/fact/cli/supervisor.py`) starts each child in its own process group (Windows) or session (POSIX), so the console's Ctrl-C reaches only `fact dev`. On a child exit or stop signal it stops the children sequentially in reverse start order (worker, API, Temporal), escalating interrupt, terminate, kill with 10 s per step, and ignores further stop signals until done. On Windows the interrupt is `CTRL_BREAK_EVENT`, so `python -m fact.worker` installs a SIGBREAK handler that drains workers like Ctrl-C.
**Why:** If all children shared the console group they would receive Ctrl-C at once and Temporal could exit before the worker drained. Reverse order lets the worker finish against a live Temporal and API. Ignoring repeated stop signals during shutdown keeps a second Ctrl-C from abandoning the children.

### [2026-11-24 18:00] API base URL is a Settings property

**What:** `Settings.api_url` (`@core/src/fact/core/settings.py`) derives the client-facing URL from `api.host`/`api.port`, mapping wildcard bind hosts to loopback and bracketing IPv6. It replaces `api_base_url()` that lived in the worker's `__main__.py`.
**Why:** The worker and the CLI both need it; one definition in `fact.core` keeps them consistent. A bind address like `0.0.0.0` is valid for the server but not as a client target.

### [2026-11-24 18:00] `fact dev` passes `--port` to the Temporal dev server

**What:** `fact dev` runs `temporal server start-dev` with `--port` taken from `settings.temporal.address`, in addition to `--db-filename` and `--ui-port 8233`. The worker waits for that same host and port before starting.
**Why:** Without `--port` the dev server always listens on its default 7233, so a changed `temporal.address` would point the worker at nothing. Settings stay the single source for the address.

### [2026-11-25 12:00] The task loop reopens a task whose run hit the usage limit, and resumes work after a restart

**What:** `@scripts/task_loop.py` no longer trusts a commit made by a run that hit the Claude usage limit (rejected, or a usage window at 100%). `@scripts/loop_recovery.py` restores the task file from before the task, adds `resume: "<start>..<end>"` (the unverified range; `start` is kept across repeated reopens) and a `## Resume` section, and commits it as `chore(tasks): reopen <id> to verify <range> after the usage limit`. Reopened tasks sort first (`@scripts/task_files.py`, `@.agents/skills/next-task/next_task.py`), and at most 3 reopens in a row are allowed. The task in progress (id, path, `head_before`, `hit_limit`, `reopens`) is persisted in `.tmp/task-loop/state.json`; on start, a dirty tree with no new commit whose task is still the top runnable one is resumed on top of the partial work (Claude gets a system-prompt note), a deleted-but-uncommitted task file is restored, a commit after a limit hit is reopened, and any other dirty tree stops the loop.
**Why:** A run cut off by the limit can still commit, so its output may be incomplete while the task file is already deleted. Reopening through a normal commit keeps the history honest and lets a fresh run verify the range instead of rewriting it. Persisting state lets a restart, including one during the wait for a usage reset, continue the same task rather than discard or duplicate work; the reopen cap stops a task that keeps hitting the limit from looping forever.

### [2026-11-25 18:00] Subagents are canonical in `.agents/agents`, projected into `.claude/agents`

**What:** Subagent definitions moved from `.claude/agents/` to `.agents/agents/` (same `core/`, `backend/`, `frontend/`, `python/` layout, Claude Code markdown-with-frontmatter format). The SessionStart hook `@.claude/hooks/project_dot_agents.py` (formerly `link_skills.py`) projects both `.agents/skills` and `.agents/agents` into `.claude/skills` and `.claude/agents`: a symlink or junction, else a managed copy; an unmanaged or locally edited target is moved to `.bak-<timestamp>`, never deleted. It prints `PROJECTION_SKILLS=` and `PROJECTION_AGENTS=` with the mode used. `.claude/agents` and `.claude/agents.*` are gitignored.
**Why:** Skills and subagents live together under one tool-neutral `.agents/` tree, so the canonical files do not depend on any one harness. No cross-tool standard for subagent files exists, so the folder and format are our own convention (Claude's, the only harness we run today). Projecting instead of duplicating keeps a single copy that is edited and committed.

### [2026-11-25 20:00] Claude Code adapter: usage, stop reason, arguments and environment

**What:** `ClaudeCodeHarness` (`@worker/src/fact/worker/harnesses/claude_code.py`, M1-08) sets `tokens_in` to `input_tokens + cache_creation_input_tokens + cache_read_input_tokens` and `tokens_out` to `output_tokens` from the final `result` message; `stop_reason` is that message's `subtype`, not `terminal_reason`. Allowed tools go after a single `--allowedTools` flag, one argument per rule. When `claude` resolves to a `.cmd`/`.bat` wrapper, an argument containing a cmd.exe metacharacter raises `HarnessError` instead of being escaped. The process gets the minimal environment from `build_env` plus `request.env`.
**Why:** With prompt caching most input arrives as cache tokens, so `input_tokens` alone understates usage. `subtype` is the documented field; `terminal_reason` is newer. One argument per rule keeps rules containing spaces or commas intact without quoting. Escaping for cmd.exe is error-prone and a mistake there is command injection, so refusing is safer. A minimal environment keeps the worker's own secrets out of the agent; the caller passes exactly the credentials the step needs.

### [2026-11-25 22:00] Routing resolves harness and model per field

**What:** `choose` (`@worker/src/fact/worker/routing.py`) takes each of `harness` and `model` from the first entry that sets it, in order: step override, `<complexity>.<stage>`, `<complexity>.*`, `*.<stage>`, else `routing.default`. Rules and overrides are partial `RouteOverride`s.
**Why:** A rule such as `*.review: {model: opus}` should change only the model and keep the harness chosen elsewhere. Whole-entry matching would force every rule and override to repeat both fields, and a model-only override would silently reset the harness to the default.

### [2026-11-25 22:00] `step_defaults` is a typed model per step kind, not a dict

**What:** `StepDefaultsSettings` (`@core/src/fact/core/settings.py`) has typed fields per kind (`agent`: 60 min, `max_turns` 100; `shell`: 15 min), each a `StepDefaults` subclass with its own defaults; kinds without a field get `StepDefaults()` through `Settings.step_default(kind)`.
**Why:** With a dict, a YAML file setting only `agent.max_turns` would replace the whole entry and lose the agent timeout, or drop the other kinds entirely. Typed fields merge partial YAML with per-kind defaults and validate values at load time.

### [2026-11-25 22:00] Agent outputs come from `.fact-out/<stage_id>.json` at the workspace root, capped at 64 KiB (superseded below)

**What:** `agent_step` (`@worker/src/fact/worker/activities/agent.py`) reads the step's outputs from `.fact-out/<stage_id>.json` in the workspace root, removes it before and after the run, and fails the step (`failed_error`) if it is not a JSON object or is over 64 KiB.
**Why:** The workspace root is outside every repository worktree, so the agent cannot commit the file by accident. One file per stage keeps a previous stage's or attempt's outputs from being read. Outputs travel in the Temporal payload, so the cap keeps large data in the artifact store instead.

### [2026-11-25 22:00] Prompts are read from the run's branch in the home repository

**What:** `agent_step` renders `.fact/prompts/<prompt_name>.md` from the home repository's worktree, on the run's branch, the same way `.fact/repo.yaml` is read. `home_repo` defaults to the first repository in the workspace handle. A missing prompt or home repository, an unsafe name or an unknown placeholder is a non-retryable `ApplicationError`.
**Why:** A prompt change travels with the code change on the same branch and is reviewed in the same PR, and an in-flight run keeps the prompts its branch had. The first repository is the home repository for single-repo projects, so callers pass `home_repo` only for multi-repo runs. These errors are configuration mistakes that retrying cannot fix.

### [2026-11-25 22:00] Hitting the turn limit is `stopped_by_monitor`, not `failed_error`

**What:** `agent_step` maps a harness stop reason of `error_max_turns` to `StepOutcome.STOPPED_BY_MONITOR`; other error results and `HarnessError` are `failed_error`.
**Why:** The turn limit is a guard fact imposes (`step_defaults.agent.max_turns`), not an agent or harness failure, so workflows and reports can tell "stopped by our limit" (retry with a higher limit, or escalate) apart from "the agent broke".

### [2026-11-25 23:00] `github_app_id` is a string

**What:** `Settings.github_app_id` is `str | None`, and `GitHubApp` puts it in the JWT `iss` claim as a string.
**Why:** GitHub accepts either the numeric App ID or the Client ID as the issuer. An `int` field would reject the Client ID.

### [2026-11-25 23:00] Installation tokens are scoped to one repository

**What:** `GitHubApp.installation_token(owner/repo)` requests the token with `{"repositories": [repo]}` and caches it per repository (case-insensitive key) until 5 minutes before expiry. A per-repository `asyncio.Lock` makes concurrent callers mint once.
**Why:** An agent or step holding a token can then only reach the repository it works on, even if the App is installed on many. The cost is one token per repository instead of one per installation. The refresh margin keeps a token from expiring in the middle of a push or a long API sequence.

### [2026-11-25 23:00] Bot identity lookup is unauthenticated; GitHub caches live per `GitHubApp` instance

**What:** `bot_identity()` reads `GET /users/<slug>[bot]` without credentials to build the `<id>+<slug>[bot]@users.noreply.github.com` commit email. The token, app info and bot identity caches are attributes of the `GitHubApp` instance, not module globals; callers keep one instance per process.
**Why:** The user lookup is public, and an installation token would need a repository to scope it to. Instance caches keep tests isolated (each test builds its own App with a mock transport and clock) and avoid hidden global state; the price is that building a new `GitHubApp` per call mints a new token each time. The noreply domain is github.com's, so GitHub Enterprise Server is not supported yet.

### [2026-11-25 23:30] The push token reaches git only through `GIT_CONFIG_*` env, with overrides that neutralize worktree config

**What:** `push_branch` (`@worker/src/fact/worker/git_push.py`) pushes `refs/heads/<branch>` to an explicit URL, not the worktree's `origin`, with `--no-verify`. The installation token is passed only as `http.<url>.extraHeader` (Basic `x-access-token`) through `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`/`GIT_CONFIG_VALUE_n`. The same environment sets `core.hooksPath` to an empty temporary folder, `core.fsmonitor=false`, empty `credential.helper`, `core.askPass` and `http.proxy`, `http.sslVerify=true`, `protocol.allow=never` with only the URL's scheme allowed, plus `GIT_CONFIG_NOSYSTEM=1` and `GIT_TERMINAL_PROMPT=0`. Output lines are redacted of the raw token and its Basic value.
**Why:** The agent controls the worktree, including its git config and hooks, while the push runs in the control zone holding the token. Environment config outranks every config file, so it overrides whatever the agent wrote: no hook or fsmonitor can run code, no helper, askpass or proxy can capture the credential, and an `insteadOf` rewrite cannot reach `ext::` or another protocol. Scoping the header to the URL keeps it from any rewritten host. Rejected: a token in the remote URL or argv (visible in process lists and error output) and writing it to a config file (persists on disk where the agent can read it).

### [2026-11-25 23:30] The mirror's `core.bare` lives in its `config.worktree`

**What:** On every sync, `LocalWorktreeRunner` (`@worker/src/fact/worker/runners/local_worktree.py`) enables `extensions.worktreeConfig` and moves `core.bare = true` from the bare mirror's shared `config` to its own `config.worktree`.
**Why:** With `extensions.worktreeConfig` on (needed for per-worktree `user.name`/`user.email`), the shared `core.bare = true` applies to every linked worktree too, so `git add` and `commit` there failed with "must be run in a work tree". Moving the key keeps the mirror bare and the worktrees normal. Doing it on every sync rather than only at clone time repairs mirrors created before the fix.

### [2026-11-25 23:30] `open_change_set` pushes the branch named in its input, not the worktree's HEAD

**What:** `OpenChangeSetInput.branch` is the run's branch from its `RepoCheckout`. The activity requires the worktree to have exactly that branch checked out (a detached HEAD or another branch is a non-retryable error), refuses the repository's default branch, and pushes only `refs/heads/<branch>`.
**Why:** The agent can switch branches in its worktree. Reading the branch from HEAD would let it choose what the control zone pushes with a write token, including the default branch. Pinning the branch from workflow input keeps the decision in deterministic workflow state; a mismatch signals a bug or a tampered worktree, which retrying cannot fix.

### [2026-11-25 23:59] The activity contract lives in `fact.core`, and workflows call activities by name

**What:** Task queue names, activity names and activity input models moved from the worker to `@core/src/fact/core/activity_contract.py`. `FactWorkflow` (`@sdk/src/fact/sdk/workflow.py`) calls `workflow.execute_activity` with the string names; the worker registers its activities under the same names and re-exports the old import paths.
**Why:** Project workflows import `fact.sdk`, and the planned per-project workflow worker loads them in Temporal's sandbox. If `fact.sdk` imported `fact.worker`, workflow code would pull in runners, harnesses, httpx and the GitHub App, breaking the dependency direction and sandbox imports. Calling by name also lets tests register fake activities under the real names.

### [2026-11-25 23:59] Fix sessions are attempts of the `implement` stage; attempt numbers are per stage

**What:** `until_green` runs each fix session as a new attempt of `fix_stage` (default `implement`), and the `checks` stage declares `loops_to="implement"`. `FactWorkflow.next_attempt(stage_id)` counts per stage, so every shell step in `checks` (each check in each repository, every round) takes the next attempt number of `checks`.
**Why:** Steps are unique by (run, stage, attempt) in the runs API, so every activity call needs its own attempt within its stage. Treating a fix as another implement attempt matches the manifest's loop edge, so the UI shows "implement ran 3 times" (spec `10-observability.md`) instead of an extra stage. Rejected: one attempt number per round shared by all checks, which would collide when lint and test both run in a round.

### [2026-11-25 23:59] `max_attempts` caps fix sessions, and a failed fix session ends the loop

**What:** `until_green(..., max_attempts=N)` allows at most N fix sessions, so checks run at most N + 1 times. If a fix session itself does not pass (an error or the turn limit), the loop stops and returns `failed_checks` without re-running checks.
**Why:** Counting fixes rather than check runs makes the number the one that costs money (agent sessions) and reads naturally in the run summary ("still failing after 3 fix sessions"). A session that errored or hit its limit is unlikely to have fixed anything, so another round would only spend more.

### [2026-11-25 23:59] New run outcomes `pr_opened` and `needs_human`

**What:** `RunOutcome` gains `pr_opened` (the run ended with its change set open for review) and `needs_human` (the run gave up, such as exhausted checks). `Standard` ends `pr_opened` when green, `needs_human` with no PR when checks never pass, and `failed` when implement does not pass.
**Why:** M1 has no merge stage, so a successful run cannot be `merged`; recording it as `failed` or leaving it open would be wrong. Exhausted checks are not a failure of the factory but a request for a person, and M2 turns that point into a gate, so it needs its own outcome to count and notify on. Not opening a PR for red checks keeps reviewers from seeing unverified changes.

### [2026-11-25 23:59] Run bookkeeping and `mark_task_done` are activities that are not reported as steps

**What:** `record_run_started`, `record_run_finished` and `mark_task_done` (`@worker/src/fact/worker/activities/control.py`) run on the control queue with `INFRA_RETRY` but create no step row. `FactWorkflow.execute` treats a failed `record_run_*` as a warning. `mark_task_done` commits only the task file on the run's branch, with hooks, fsmonitor and signing off, and is idempotent on retry.
**Why:** These are bookkeeping, not work a reviewer inspects; as steps they would clutter the run graph and the manifest. Recording is best effort so an API outage cannot fail a run that did its work. Committing the task's `status: done` on the run's branch lands the work and its completion as one squash-merged PR. The control zone must not run worktree code, hence the git hardening; idempotence matters because a retry after a commit must not commit twice or fail.

### [2026-11-25 23:59] Workflow timeouts travel in `RunInput.limits`

**What:** `RunInput.limits: RunLimits` carries the agent, shell and control activity timeouts in seconds; whoever starts the run is meant to fill them from `step_defaults` (no starter exists yet; the defaults are 3600 s, 900 s and 600 s). `FactWorkflow` reads only these, never settings.
**Why:** Workflow code is replayed and must be deterministic; reading settings would let a config change alter scheduling decisions on replay. Pinning limits in the input also keeps a run's timeouts fixed for its whole life.

### [2026-11-25 23:59] Pinned runs use Temporal Worker Deployment Versioning, one deployment per project

**What:** Each registered project's workflows run on their own worker, task queue and deployment `wf-<project>` (`@worker/src/fact/worker/deployments.py`), with `use_worker_versioning=True` and default behaviour `PINNED`. On start the worker calls `SetWorkerDeploymentCurrentVersion` for its build id (retried while the server registers the version) and startup fails if that never succeeds. A worker process runs only the newest version; side-by-side versions (`fact upgrade`) are deferred.
**Why:** The spec requires that a run finishes on the version it started on (`docs/spec/06-workflow-authoring.md`). The server enforces this with `PINNED`, so fact does not need its own routing or a recorded-only version that workers could ignore. One deployment per project lets each home repository move its version independently. Making the version current on start means new runs reach the new code without a manual step, and failing startup is better than a worker that polls while new runs never start. The cost: after a restart on new code, runs pinned to an older version wait until they are dropped and restarted.

### [2026-11-25 23:59] The version id keeps 12 characters of the factory SHA and the whole home SHA

**What:** The build id is `<factory SHA[:12]>+<home SHA>` (`version_id` in `@worker/src/fact/worker/loader.py`), or `unknown+<home SHA>` when the factory is not a git checkout. `record_run_started` stores it as `worker_version` and the home part separately as `home_sha`. The API's `RunUpdate` caps both at 64 characters.
**Why:** Two full SHAs plus the separator take 81 characters, over the existing 64-character `worker_version` column. Shortening the factory part fits the column without a migration. The home SHA is the one the run's workflow code is checked out from and must be exact; the factory SHA only identifies the fact build, where 12 characters are unique in practice.

### [2026-11-25 23:59] Workflow modules load under unique names through a meta path finder, not as passthrough modules

**What:** `.fact/workflows/*.py` is imported as `fact_workflows_<project>_<sha12>_<stem>` through a `MetaPathFinder` appended to `sys.meta_path` that maps each name to its file in the pinned checkout. Two files mapping to the same name are refused. Only `fact`, `pydantic`, `pydantic_core` and `rich` are sandbox passthrough modules (`sandbox_runner()`).
**Why:** Temporal's sandbox re-imports a workflow's module by name for every run, so the module must be importable by name from inside the sandbox. A finder does that without adding checkout folders to `sys.path`, where every project's `standard.py` would clash. Names unique to project and SHA keep two projects, or two versions of one, from sharing a module. Rejected: making the workflow modules passthrough, which would share one module instance across runs and drop the sandbox's isolation of workflow code.

### [2026-11-25 23:59] The run row is reserved before the workflow starts, and kept as failed if the start fails

**What:** `POST /api/v1/runs/start` (`@api/src/fact/api/routers/run_start.py`) inserts the run as `queued` under workflow id `run-<safe task key>-<n>`, with `n` one past the highest used for that task, and only then calls `start_workflow` on `wf-<project>`. A unique-constraint clash on insert takes the next `n`. If the start fails, the row is kept and marked `finished`/`failed`; on `WorkflowAlreadyStartedError` it moves on to the next `n`. This reverses the order in the M1-14 task text (start, then record). It also makes `RunInput.limits` filled from `step_defaults` by this starter.
**Why:** The workflow's first activity, `record_run_started`, PATCHes the run row, so the row must exist before the workflow can run. Keeping a failed row keeps its number used, so a workflow id is never reused, and a start that timed out but did reach Temporal still finds its row and updates it. Rejected: deleting the row on failure, which would free the number while a workflow with that id might exist.

### [2026-11-25 23:59] Run start reads tasks from `origin/<default branch>` with `git archive`

**What:** Before starting a run, the API runs `git fetch origin <default branch>` in the project's local checkout (`GIT_TERMINAL_PROMPT=0`, `GCM_INTERACTIVE=never`, 60 s timeout; a failed fetch logs a warning and falls back to the last fetched state), then extracts the task folder from `origin/<default branch>` with `git archive` into a temporary directory and reads it with `FileTaskSource` (`@api/src/fact/api/task_ref.py`). The task and its dependency statuses come from there, never from the working tree.
**Why:** A run works from the default branch, and dependencies count as done only once merged; the developer's working tree may hold unmerged or uncommitted task edits that would let a run start too early or on a task that does not exist upstream. `git archive` reads a ref without touching the checkout or its index, so the API never disturbs the developer's tree. Disabling prompts keeps an API request from hanging on credentials, and the offline fallback still lets runs start from the last known state.

### [2026-11-25 23:59] The pinned factory is a detached worktree of the source repo, run with `uv run --frozen` and the source tree's config

**What:** `fact use <ref>` (`@cli/src/fact/cli/use.py`, named `fact release` until it was renamed) adds a detached worktree of the factory's own source repository (the checkout the CLI runs from), not of the bare agent mirror under `repos_dir`, at `pinned_dir/factory/<sha>`, runs `uv sync --locked` there and records the SHA in `pinned_dir/current`. `fact dev --pinned` (`@cli/src/fact/cli/dev.py`) starts the API and worker as `uv run --frozen --project <checkout> python -m ...` with `FACT_CONFIG` pointing at the source tree's `fact.yaml`. `LocalWorktreeRunner` refuses a workspace inside `pinned_dir` or inside the running factory checkout.
**Why:** The source repo already has the developer's fetched refs, and `origin/<ref>` is resolved after a fetch, so a pin can be any branch, tag or SHA without touching the mirror agents push from; the mirror stays owned by the runner. A pinned checkout has no git-ignored `.env`, so pointing `FACT_CONFIG` at the source tree makes the pinned checkout read the same `fact.yaml`, `.env` and secrets (`.env` is read next to `fact.yaml`) instead of copying them. `--frozen` skips a second lock check and sync on every start: the environment was already synced against the lockfile with `--locked` when the version was pinned. The runner check turns "agents never edit the running factory" from a convention into an enforced error.

### [2026-11-25 23:59] `fact release` is renamed `fact use`, and `--release` is `--pinned`

**What:** The command that checks out the factory version to run is `fact use <ref>` (`@cli/src/fact/cli/use.py`), `fact dev --pinned` runs it, and the derived path is `Settings.pinned_dir` (`<data_dir>/pinned`, was `release_dir`, `<data_dir>/release`), which also holds the worker's per-project checkouts (`pinned_checkout` in `@worker/src/fact/worker/loader.py`). Plain `fact dev` still runs the source tree.
**Why:** In most CLIs "release" means publishing (a tag, a package, a GitHub release); this command publishes nothing, it switches the local version the factory runs, like `nvm use`. Keeping plain `fact dev` on the source tree keeps the default unsurprising when developing fact by hand. Rejected: `fact self update` (reads as fetching the latest only), `--stable` (a pin can be any branch). Old checkouts under `<data_dir>/release` are not migrated: run `fact use <ref>` again.

### [2026-11-25 23:59] Agent sessions run headless in `auto` permission mode and end with a schema-checked answer

**What:** `ClaudeCodeHarness` always passes `--permission-prompts none` and, from `step_defaults.agent.permission_mode` (default `auto`), `--permission-mode`. `agent_step` passes `--json-schema` (`BASE_OUTPUT_SCHEMA`: `status: done|blocked`, `summary`, `blocker`, extended by `.fact/prompts/<name>.schema.json`) and decides the outcome from the structured answer: `done` is `passed`, `blocked` is `needs_human`, no answer is `failed_error`. Denied tool calls are logged and kept in `outputs.permission_denials`. This replaces `.fact-out/<stage_id>.json`; the `Standard` workflow ends `needs_human` with no PR when implement is blocked.
**Why:** The first self-run (PR #4) ran without a permission mode: every write was refused, the session still ended `success`, so the step passed and fact opened a PR with only the "mark done" commit. Judging by exit code cannot tell "did the work" from "explained why it could not". `auto` keeps permission checks on, as `local-worktree` requires (`docs/spec/09`), without anyone to answer prompts; `bypassPermissions` stays for the isolated Docker runner.
