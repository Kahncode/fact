# Runners and harnesses

A runner pool decides where a run's sandbox lives and a harness decides which coding agent runs in it; both sit behind small interfaces so new ones plug in without touching workflows.

**Runner interface** (`fact.worker.runners.base.Runner`): `prepare(workspace_spec) -> handle`, `exec(handle, argv, cwd, env, timeout) -> stream`, `teardown(handle)`, `capabilities()`. `argv` is an argument list, never a shell string. `cwd` is relative to the workspace root and must stay inside it. The stream yields `ExecLine` items (merged stdout and stderr) and then exactly one `ExecExit` (`exit_code`, `timed_out`; exit code -1 on timeout). `teardown` is safe to call twice.

**`local` (built, M1-06; worktrees until 27 Nov 2026)**: clones each repository in full from its remote into `workspaces_dir/<run_id>/<repo name>`, so repo names in one run must be distinct. A workspace shares no git state with another (its own `.git`, hooks and config), so runs of one repository never step on each other, and its setup can install hooks pinned to its own environment. The task branch is resumed from `origin/<branch>` when it exists, otherwise cut from `origin/<default_branch>`; a task has one active run at a time, so the branch is always the task's own, and `WorkspaceHandle.branches` carries it for `open_change_set`. A workspace left behind by a failed run keeps its unpushed commits in its own clone; the task's next run starts again from the remote branch. `prepare` returns a workspace an earlier attempt of the same run finished (marked by `WORKSPACE.md`), and otherwise removes any half-made folder of the run id and starts over. Overlapping attempts of a run coordinate through a file lock on disk per run folder, never in-process locks, so it holds across worker processes (details in `worker/README.md`). `prepare` writes `WORKSPACE.md` (folder, repository, role, branch) in the run folder and sets the commit author to the GitHub App's bot user (`fact[bot]`). The factory is meant to always commit as `fact[bot]`; `commit_author_name` and `commit_author_email` are only a fallback for local runs without the App (or when the bot lookup fails), and should rarely be used. Commands inherit the worker's environment plus what the caller passes: the local machine is trusted, and isolation is the container runner's job, not this one's. On timeout or cancel the whole process tree is killed; on Windows a descendant of an already-dead leader cannot be reached by `taskkill /T`. Run ids, repository names and branch names are validated, and teardown derives the run folder from the run id, not from the handle.

| Profile | Isolation | When | Phase |
| --- | --- | --- | --- |
| `local` | A full git clone per repository in `~/.fact/workspaces/<run>`; runs as your user | Trusted repos, permission checks left on | v0 |
| `docker` | One container per run, the workspace on a Docker volume, no host mounts, only the secrets a command needs | Every project, permission checks off; the default for fact's own project | v1 (next) |
| `remote` | Sandbox provider or an SSH box | Parallel runs, off-laptop | Later |

**`docker` (decided 28 Nov 2026; `DockerRunner` built in issue 146, see `worker/README.md`)**: the worker stays on the host and drives one container per run through the `docker` CLI (argument lists, like git); no fact process runs inside, so the sandbox never reaches Temporal, the API or the factory's secrets. Docker Desktop is a prerequisite on Windows and macOS; the worker refuses to start when a project uses this pool and `docker info` fails.

- **Sandbox image, chosen by the workflow.** A workflow class names its sandbox (`sandbox = "<name>"`, else the project's default), which is `.fact/sandboxes/<name>/Dockerfile` in the project's home repository, and may add its own `docker run` arguments. The project's `runner_pool` (`kind: docker`) holds the pool-wide defaults: resource limits (`memory` 8 GB, `cpus` 4, `pids` 4096) and extra `run_args`; the workflow's arguments come after the pool's, which come after fact's, so the most specific wins. An image is Ubuntu with `git`, `git-lfs` and the harness CLI on PATH, a non-root user `fact` (uid 1000) and the workspace under `/workspace`. Images are built when the worker loads a project version, from its home checkout at `pinned_dir/projects/<project>/<sha>` (the one its workflows load from), one per distinct Dockerfile folder, tagged `fact-sandbox/<project>/<name>:<hash of the folder>` and labelled as fact's and reused while the hash is unchanged; a version whose images fail to build is refused like an incompatible workflow, so it never becomes current. The run gets its tag in `WorkspaceSpec`, so a run never builds or picks its own image, and a PR that edits a Dockerfile takes effect once merged and pinned.
- **Container.** `prepare` creates the run's container (`sleep infinity` under `--init`) with a volume `fact-ws-<run_id>` at `/workspace`, and clones each repository inside it, full history and LFS included. `exec` is `docker exec`, starting the container first when it is stopped (after a reboot); each command runs in its own process group, and a timeout or cancel kills that group with a second `docker exec`, since killing the `docker` client does not stop what runs inside. `teardown` removes the container and its volume; a failed run keeps both, like a `local` workspace, and `fact cleanup` removes the ones whose run has ended, and images no remaining version names (`14-dogfooding-and-safety.md`, Disk cleanup).
- **Hardening (fact's defaults).** Non-root user, `--cap-drop ALL`, `--security-opt no-new-privileges`, the resource limits above, no Docker socket, no bind mounts: nothing of the host's disk is visible. The root filesystem stays writable, so an agent installs tools into its home (uv, npm, pipx), not with `apt`. A project's `run_args` can loosen any of this: project config is the organization's, and trusted.
- **Every workspace step runs inside**: agent sessions, checks and `Script` steps, `sync_base`, the commit of `mark_task_done`, `classify_signals` (its diff), and the push. Git runs as in any clone, so the repository's `pre-commit` hooks run at each commit. The push runs `git lfs push` for the branch first (LFS uploads through a pre-push hook), then `git push --no-verify`, so the repository's pre-push hooks never run: a project's checks belong in `pre-commit` hooks or check steps.
- **Tokens.** The GitHub App key never leaves the host. Each command gets a fresh installation token in its environment, limited to the run's repositories: `contents: read` for every command (clone, fetch, LFS downloads, agent sessions), `contents: write` (and nothing else) only for the push command. Assume the sandbox can read any token a command received (`14-dogfooding-and-safety.md`, Known gaps). Projects should add a ruleset forbidding force-pushes, branch deletion and tag creation; fact does not create it.
- **Claude Code.** It authenticates with `CLAUDE_CODE_OAUTH_TOKEN` (made once with `claude setup-token`, kept in `.env`); `~/.claude` is never mounted. `CLAUDE_CONFIG_DIR` is on the run's volume, so its sessions survive a container restart and `--resume` works. Agent steps run with `--permission-mode bypassPermissions`, which only this pool accepts: the container is the boundary, and nothing waits on a prompt either way. The usage gate keeps reading the host's Claude Code login (same account).
- **Network.** Containers use Docker's default bridge with open egress for now (`14-dogfooding-and-safety.md`, Known gaps).
- **Tests.** The runner's real-Docker tests skip when Docker is not installed; CI on Linux always runs them.

**Planned pools: designed now, not built in v0.** GitHub runners and Daytona plug in behind the same runner interface. One rule makes remote pools work: a sandbox is disposable, and a run's state lives in its branches and in object storage, so any sandbox can be recreated from them.

|  | GitHub runners | Daytona |
| --- | --- | --- |
| Style | Worker inside the sandbox: a GitHub Actions job starts a factory worker that polls the run's own task queue (`sandbox-<run_id>`) | Driven from outside: a pool worker the factory hosts (not the control worker) creates the sandbox and runs commands in it through Daytona's SDK |
| Start | The factory dispatches a runner workflow (`workflow_dispatch`) in a dedicated runner repository | API call to create a sandbox from a prepared image per project |
| Lifetime | Jobs are time-limited (6 h on GitHub-hosted runners); a long run gets a new job, rebuilt from its branches | Stopped when idle and resumed on the next step; deleted when the run ends |
| Concurrency cap | The account's concurrent job limit, and our own cap below it | The account's sandbox quota, and our own cap below it |
| Secrets | Actions secrets on the runner repository (agent credentials only) | Injected as environment variables per sandbox (agent credentials only) |
| Good for | Free or included minutes, Linux builds close to CI | Fast start, many parallel sandboxes, long-lived interactive sessions |

**Prerequisites for any remote pool**: Temporal and the factory API reachable from outside your PC, and object storage that is not a local folder: M5 provides both (`19-cloud.md`, which also covers how a runner connects, authenticates and gets its credentials). Until then, the only pool is your PC.

**Harness contract**

- In: prompt, workspace path, model, allowed tools, budget, optional session id to resume.
- Out: session id, final message, exit status, tokens in and out, cost in USD, transcript path.
- Claude Code: `claude -p --output-format stream-json`, which streams messages and reports cost. Codex: `codex exec --json`; when it reports no cost, the step's cost is unknown (NULL): fact keeps no price table.
- v0 calls the CLIs as subprocesses so all harnesses look the same. Temporal's Claude Agent SDK plugin is evaluated during M1.
- **Claude Code adapter (built, M1-08)**: `fact.worker.harnesses.ClaudeCodeHarness` sends the prompt on stdin, writes every stream-json line to the transcript, and takes the result from the final `result` message: `stop_reason` is its `subtype` (`success`, `error_max_turns`, ...) or `no_result` when none arrived, and tokens in include cache writes and reads. A session can send several `result` messages: Claude Code wakes a stopped session when a background task it started finishes, and each wake ends with its own `result`, whose `usage` and `num_turns` cover only that part while `total_cost_usd` and `modelUsage` cover the whole session. So tokens come from the final `result`'s `modelUsage` (summed over its models), turns are `num_turns` summed over every `result`, peak context is the largest input (uncached + cache read + cache write) of any assistant message, every `result` is logged, and the outcome comes from the final one. It runs with the worker's environment plus the request's `env`, and with Claude Code's auto-memory off (`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`) so no notes carry between runs or projects. It runs headless with `--permission-prompts none` (nothing waits on approval; refused calls come back as `permission_denials`), `--permission-mode` from `step_defaults.agent.permission_mode` (default `auto`, which keeps permission checks on: a classifier approves or denies each call), and `--json-schema` so the session ends with a structured answer (`status: done|blocked`, `summary`, plus per-prompt fields). The step's outcome comes from that answer, not from the exit code. A session is resumed (`--resume`) at most once: when fact's worker stopped or died during it, or when the agent answered `blocked` with `retryable: true`; a session that ended without an answer, hit its turn limit or timed out is a failure and is not retried.

**Long agent steps**: the activity heartbeats every 30 s with progress, has a start-to-close timeout per step, and kills the agent process on cancel. Budgets apply per step and per run; going over pauses the run and notifies you.

### Choosing a harness and model

Every agent step runs on a harness and model chosen at run time, never hard-coded in a workflow, so moving from Claude Code to OpenCode or Pi is a config change.

- **Complexity level**: the planner (or triage, when the issue skipped planning) gives each task a level: `trivial`, `standard` or `complex`. It is stored on the run and shown in the UI.
- **Routing table** in the project's `.fact/project.yaml` maps level and step kind to a harness and model, for example `standard.implement → claude-code / sonnet`, `complex.implement → claude-code / opus`, `*.review → a different model from implement`.
- **Overrides**, highest first: a step-level choice the planner wrote for this task, then the task's level through the routing table, then the workflow's default for that step, then the project's default.
- **Availability**: if the chosen harness is over its cap, out of budget or outside its usage window, the step waits; it falls back to the next entry in its routing list only where the table allows it.
- Every step records the harness and model it actually used, so cost and success can be compared across them later.
- **Built (M1-09)**: `fact.worker.routing.choose` resolves harness and model separately: each comes from the first entry that sets it among the step override, `<level>.<stage>`, `<level>.*`, `*.<stage>`, else `routing.default`, all read from the run's project `routing` in `.fact/project.yaml` (carried on `RunInput.routing` since #86; a project without one gets `claude-code`/`sonnet`). Workflow defaults are not wired in yet.

**Harness configuration per step (planned).** The workflow chooses its sandbox (image and `docker run` arguments, above); each step chooses its harness and that harness's configuration, including extra command-line arguments, with a project default in `step_defaults` and a step-level override. Until a second harness exists, the harness is Claude Code and its arguments are fixed in the adapter.

v0 ships the Claude Code adapter and the routing table with Claude models only; the Codex, OpenCode and Pi adapters follow the same contract.

### Execution zones

A run's coding work happens in one sandbox, and trusted steps run in the control zone. Each zone is a Temporal task queue, so a step reaches its zone by the queue it is sent to.

| Zone | Runs | Holds secrets |
| --- | --- | --- |
| Control | Workflow code, planning, triage, classification (its decision, not the diff), the issue tracker, GitHub, merges, notifications, preview deploys | Yes: tracker, GitHub, deploy keys |
| Run sandbox (one per run) | Every agent session, lint and tests, and every git step on the run's clone (`sync_base`, `classify_signals`'s diff, `mark_task_done`'s commit, `push_branch`) | Agent credentials, a read token; a write token for the push command only |

Agents commit locally and never push; a framework step pushes and the control zone opens the PRs. The push is a command in the sandbox (`push_branch`, a workspace-zone activity, through `Runner.exec`) with either pool: with the `local` pool the sandbox is a clone on your PC and the same command runs on the host (decided 28 Nov 2026, #147: one code path for both pools); with the `docker` pool it runs in the run's container. It is given a `contents: write` token for that command alone (decided 28 Nov 2026: LFS objects travel with a normal push and with nothing else); agent sessions only ever get a read token. `open_change_set` then reads the pushed branch from GitHub and never opens the clone. Agents still write the words: each agent session writes its own commit messages, and the last passing session before PRs (in `Standard`, the `learn` session, else `implement`) writes a PR title and summary as a step output, which the control zone puts at the top of the PR description. With remote pools, the sandbox exports its commits as a git bundle to object storage and the control zone pushes from that; cloning private repositories works the same way in reverse, with the control zone preparing the workspace or handing over a short-lived read-only token.

**The factory orchestrates; runners do the work.** The factory side does as little as possible, so it still holds with thousands of runners.
- **Factory side:** workflow workers, plus stateless control workers that scale horizontally and hold the factory's credentials. Factory secrets never reach a runner.
- **Runners:** everything that touches a workspace (git, agent sessions, check commands and project `Script` steps).
- **Long waits:** a long wait (CI, a PR's review) is a workflow timer plus a short check activity on each poll, never one activity held for the whole wait, so it holds no worker slot.
- **Routing:** a workspace handle names the task queue of the runner that holds it, and every step on that workspace is routed to that queue.
- **Self-contained runners (M7):** a runner lives on its own infrastructure and reaches the factory only through the API and the artifact store.

**Step protocol (project `Script` steps).** The generic step activity runs the declared command in the runner, in the workspace unless the step is `workspace=False`, with these environment variables:
- `FACT_INPUT`: a JSON file holding the step's input.
- `FACT_RESULT`: the path where the script writes its JSON result. The framework validates the result against the declared output model.
- `FACT_EVENTS`: an append-only JSON-lines file the script may write to, to record outputs (`{"output": {"kind": "deployment", "url": "..."}}`), notifications and progress.

stdout and stderr are the step's log. The exit code is the outcome:
- `0`: passed.
- `2`: `needs_human`.
- Anything else: failed.

The script imports no fact library, so it can be in any language. The protocol is part of fact's version (`06-workflow-authoring.md`, Versions).

**Ready for later, not built in v0**: every sandbox has its own task queue, so a run can open several sandboxes (one per repository, or per subtask) and run them in parallel as child workflows, with the control zone fanning out and joining. Planning can move to its own sandbox the same way. None of this changes how a workflow is written, only which queue its steps are sent to.
