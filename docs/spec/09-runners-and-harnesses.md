# Runners and harnesses

A runner pool decides where a run's sandbox lives and a harness decides which coding agent runs in it; both sit behind small interfaces so new ones plug in without touching workflows.

**Runner interface** (`fact.worker.runners.base.Runner`): `prepare(workspace_spec) -> handle`, `exec(handle, argv, cwd, env, timeout) -> stream`, `teardown(handle)`, `capabilities()`. `argv` is an argument list, never a shell string. `cwd` is relative to the workspace root and must stay inside it. The stream yields `ExecLine` items (merged stdout and stderr) and then exactly one `ExecExit` (`exit_code`, `timed_out`; exit code -1 on timeout). `teardown` is safe to call twice.

**`local-worktree` (built, M1-06)**: keeps a bare mirror per repository at `repos_dir/<owner>/<repo>` (fetched on every prepare) and adds one worktree per repository at `workspaces_dir/<run_id>/<repo name>`, so repo names in one run must be distinct. The task branch is resumed from `origin/<branch>` when it exists, otherwise cut from `origin/<default_branch>`. `prepare` writes `WORKSPACE.md` (folder, repository, role, branch) in the run folder and sets the commit author from `commit_author_name` and `commit_author_email`. Commands run with a minimal environment (`PATH`, `HOME`, `USERPROFILE`, `SYSTEMROOT` plus what the caller passes). On timeout or cancel the whole process tree is killed; on Windows a descendant of an already-dead leader cannot be reached by `taskkill /T`. Run ids, repository names and branch names are validated, and teardown derives the run folder from the run id, not from the handle.

| Profile | Isolation | When | Phase |
| --- | --- | --- | --- |
| `local-worktree` | Git worktrees in `~/.fact/workspaces/<run>`; runs as your user | Trusted repos, permission checks left on | v0 |
| `local-docker` | Container with the workspace mounted and only the secrets it needs | Any step that runs an agent with permission checks off | v1 |
| `remote` | Sandbox provider or an SSH box | Parallel runs, off-laptop | Later |

**Planned pools: designed now, not built in v0.** GitHub runners and Daytona plug in behind the same runner interface. One rule makes remote pools work: a sandbox is disposable, and a run's state lives in its branches and in object storage, so any sandbox can be recreated from them.

|  | GitHub runners | Daytona |
| --- | --- | --- |
| Style | Worker inside the sandbox: a GitHub Actions job starts a factory worker that polls the run's own task queue (`sandbox-<run_id>`) | Driven from outside: the control worker creates the sandbox and runs commands in it through Daytona's SDK |
| Start | The factory dispatches a runner workflow (`workflow_dispatch`) in a dedicated runner repository | API call to create a sandbox from a prepared image per project |
| Lifetime | Jobs are time-limited (6 h on GitHub-hosted runners); a long run gets a new job, rebuilt from its branches | Stopped when idle and resumed on the next step; deleted when the run ends |
| Concurrency cap | The account's concurrent job limit, and our own cap below it | The account's sandbox quota, and our own cap below it |
| Secrets | Actions secrets on the runner repository (agent credentials only) | Injected as environment variables per sandbox (agent credentials only) |
| Good for | Free or included minutes, Linux builds close to CI | Fast start, many parallel sandboxes, long-lived interactive sessions |

**Prerequisites for any remote pool**: Temporal and the factory API reachable from outside your PC (Temporal Cloud, or a hosted server or tunnel), and object storage that is not a local folder. Until then, the only pool is your PC.

**Harness contract**

- In: prompt, workspace path, model, allowed tools, budget, optional session id to resume.
- Out: session id, final message, exit status, tokens in and out, cost in USD, transcript path.
- Claude Code: `claude -p --output-format stream-json`, which streams messages and reports cost. Codex: `codex exec --json`, with tokens priced from a table when no cost is reported.
- v0 calls the CLIs as subprocesses so all harnesses look the same. Temporal's Claude Agent SDK plugin is evaluated during M1.
- **Claude Code adapter (built, M1-08)**: `fact.worker.harnesses.ClaudeCodeHarness` sends the prompt on stdin, writes every stream-json line to the transcript, and takes the result from the final `result` message: `stop_reason` is its `subtype` (`success`, `error_max_turns`, ...) or `no_result` when none arrived, and tokens in include cache writes and reads. It runs with a minimal environment, so credentials come only through the request's `env`. It runs headless with `--permission-prompts none` (nothing waits on approval; refused calls come back as `permission_denials`), `--permission-mode` from `step_defaults.agent.permission_mode` (default `auto`, which keeps permission checks on: a classifier approves or denies each call), and `--json-schema` so the session ends with a structured answer (`status: done|blocked`, `summary`, plus per-prompt fields). The step's outcome comes from that answer, not from the exit code.

**Long agent steps**: the activity heartbeats every 30 s with progress, has a start-to-close timeout per step, and kills the agent process on cancel. Budgets apply per step and per run; going over pauses the run and notifies you.

### Choosing a harness and model

Every agent step runs on a harness and model chosen at run time, never hard-coded in a workflow, so moving from Claude Code to OpenCode or Pi is a config change.

- **Complexity level**: the planner (or triage, when the issue skipped planning) gives each task a level: `trivial`, `standard` or `complex`. It is stored on the run and shown in the UI.
- **Routing table** in `fact.yaml` maps level and step kind to a harness and model, for example `standard.implement → claude-code / sonnet`, `complex.implement → claude-code / opus`, `*.review → a different model from implement`.
- **Overrides**, highest first: a step-level choice the planner wrote for this task, then the task's level through the routing table, then the workflow's default for that step, then the project's default.
- **Availability**: if the chosen harness is over its cap, out of budget or (from v0.1) outside its usage window, the step waits; it falls back to the next entry in its routing list only where the table allows it.
- Every step records the harness and model it actually used, so cost and success can be compared across them later.
- **Built (M1-09)**: `fact.worker.routing.choose` resolves harness and model separately: each comes from the first entry that sets it among the step override, `<level>.<stage>`, `<level>.*`, `*.<stage>`, else `routing.default` in `fact.yaml`. Workflow and project defaults are not wired in yet.

v0 ships the Claude Code adapter and the routing table with Claude models only; the Codex, OpenCode and Pi adapters follow the same contract.

### Execution zones

A run's coding work happens in one sandbox, and trusted steps run in the control zone. Each zone is a Temporal task queue, so a step reaches its zone by the queue it is sent to.

| Zone | Runs | Holds secrets |
| --- | --- | --- |
| Control | Workflow code, planning, triage, classification, the issue tracker, GitHub, merges, notifications, preview deploys, `git push` | Yes: tracker, GitHub, deploy keys |
| Run sandbox (one per run) | Every agent session, lint and tests | Agent credentials only |

Agents commit locally and never push; the control zone pushes and opens the PRs, so the sandbox never holds GitHub credentials. In v0 the control zone and the sandbox run on the same machine, your PC: the sandbox is a worktree (later a container with the worktree mounted), so the control worker reads the branch and pushes from it directly. Agents still write the words: each agent session writes its own commit messages, and the last session before PRs writes a PR title and summary as a step output, which the control zone puts at the top of the PR description. With remote pools, the sandbox exports its commits as a git bundle to object storage and the control zone pushes from that; cloning private repositories works the same way in reverse, with the control zone preparing the workspace or handing over a short-lived read-only token.

**Ready for later, not built in v0**: every sandbox has its own task queue, so a run can open several sandboxes (one per repository, or per subtask) and run them in parallel as child workflows, with the control zone fanning out and joining. Planning can move to its own sandbox the same way. None of this changes how a workflow is written, only which queue its steps are sent to.
