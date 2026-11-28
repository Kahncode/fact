# fact

fact is a software factory: a service that turns tracker issues into reviewed GitHub PRs using coding agents, with Temporal running every workflow durably.

- Works on any git + GitHub project, including projects that span several repositories.
- Takes work from a task tracker (GitHub Issues first, Linear next) and has a planning loop that turns ideas into specs and issues.
- Workflows are code combining agents, deterministic tools and persistent data; they can change often without breaking runs in flight.
- Every step is inspectable: logs, agent transcript, wall time, human wait time, tokens and cost.
- Humans review in GitHub PRs; the UI follows progress and notifies you when something needs you.

## Running locally

Install the [Temporal CLI](https://docs.temporal.io/cli#install) so `temporal` is on your PATH, then:

```
uv sync
uv run fact dev
```

`fact dev` starts the Temporal dev server (UI at http://localhost:8233), the API and the worker, prefixing each output line with the process name. They run detached, tracked in `<data_dir>/run/`, so restarting `fact dev` (or it crashing) leaves them running and the next `fact dev` adopts them; a second `fact dev` is refused. A child that exits is started again. Ctrl-C stops all three, and so does `uv run fact shutdown` from another terminal (or with no `fact dev` running): it stops the workers, then the API, then Temporal, without waiting for runs to end; open runs stay in Temporal and carry on at the next `fact dev`. In another terminal, `uv run fact projects list` shows the projects the API knows about.

## Letting fact work on itself

fact's own repository is its first project, so the running factory must not be the checkout an agent edits. Pin a version and run from it:

```
uv run fact use main
uv run fact dev --pinned
```

`fact use <ref>` checks the ref out (from `origin` when it exists there) as a separate copy under `<data_dir>/pinned/factory/<sha>` and installs its dependencies; `fact dev --pinned` runs the API and worker from that copy while still reading your `fact.yaml` and `.env`. To pick up merged changes, run `fact use main` again and restart `fact dev --pinned`, but only once runs have ended: a run still in progress or waiting for review when the worker restarts on new code stalls (after merging a run's PR, wait until `fact status` shows it `merged`, within about a minute; stop the others with `fact stop`, below). See `docs/spec/14-dogfooding-and-safety.md` and `docs/ARCHITECTURE.md`.

## Starting a run

With `fact dev` running, start the project's workflow for a task and follow it:

```
uv run fact run task:M1-02
uv run fact runs
uv run fact status run-task-M1-02-1
```

A project whose `.fact/project.yaml` sets `task_source.kind: github` takes its tasks from GitHub issues instead: `uv run fact run gh:owner/repo#12` starts issue 12 (it needs the GitHub App, below). fact's own work is GitHub issues on `Kahncode/fact`.

`fact run` reads a file task from the project's default branch on `origin` (not your working tree), refuses if its dependencies are not done (`--force-deps` starts anyway) or if the task already has a run queued, running or waiting (stop it first with `fact stop`), and prints the run id, workflow id and a link to the run in the Temporal UI. Pass `--project` when several projects hold the same task key. `fact status` accepts a run id or a workflow id and shows the run's cost and tokens (input, cache read, cache write, output) and each step with its duration, outcome, cost and log links (a cost Claude Code never reported reads `unknown`, never $0), plus the gate the run is waiting at, if any, or another reason it waits (`Waiting:`).

An agent session that hits the Claude usage limit does not fail the run: the run waits until the limit resets (`fact status` shows `Waiting:  usage limit until <time> UTC`), however long that is, then resumes the same session. See `docs/spec/10-observability.md`.

When a run waits for you, answer its gate, or send instructions to its next agent session (a running session is not interrupted):

```
uv run fact gate run-task-M1-02-1 approve
uv run fact gate run-task-M1-02-1 reject --note "wrong approach, see the task"
uv run fact steer run-task-M1-02-1 "Keep the old function name"
```

`fact gate` answers the open gate; pass `--gate <id>` to answer a specific one.

To restart a task on newer code, stop its run, then start the task again:

```
uv run fact stop run-task-M1-02-1 --reason "restart on the review fix"
uv run fact run task:M1-02
```

The stopped run ends `stopped`: it leaves a handoff note (the reason, the last stage reached, the PR links, each agent session's summary), removes its workspace and keeps its branch and PR on GitHub. The next run starts from that branch, reuses the PR and gets the note in its prompts. Commits the old run never pushed are lost.

A run whose worker version no worker runs anymore never handles a stop. `fact stop <run> --force` ends it at once: it terminates the workflow in Temporal and records the run `stopped` (reason `forced`), holding the task as a stopped run does, but writes no handoff note and leaves the workspace on disk; delete the folder it prints by hand.

A project can require you to approve each task's plan before work starts: set `require_plan_approval` in its `.fact/project.yaml` (default true), or in a task's front matter to override it. The run then waits at an `approve_plan` gate; rejecting it ends the run `stopped`. fact's own project turns this off, since labelling an issue `fact:ready` already approves it.

To see what needs you, check your inbox. A run notifies you when it waits at a gate, when its PRs are ready for review, and when it fails or gives up:

```
uv run fact inbox
uv run fact inbox --all
uv run fact inbox read 3
```

`fact inbox` lists unread notifications, newest first, with a link to the PR or the run; `--all` includes those already read, and `fact inbox read <id>` marks one read. The web UI's Notifications page replaces this in M3.

## Letting fact pick up work

Instead of starting each task by hand, keep `fact dev` running: the scheduler is on at every API start and runs every ready task. Check it, or stop and resume it:

```
uv run fact scheduler status
uv run fact scheduler off
uv run fact scheduler on
```

Every 30 seconds it starts the open issues labelled `fact:ready` (or, for a project using task files, the tasks marked `status: ready` on the default branch) whose dependencies are done, shallowest dependencies first, at most `runner_pool.max_concurrent_runs` runs at once per project (default 2, in its `.fact/project.yaml`).

- A sub-issue starts only when it and every parent above it are labelled `fact:ready`, so a plan under a `fact:planned` parent waits until you set the parent `fact:ready`. A parent waits for its open sub-issues; once none is left open, a parent with a work type (`type:feature`, `bug`, `chore`, `incident`) runs as that type and one without is closed as completed without a run (`type:epic` is for people; fact ignores it). `fact run` refuses an issue whose parent is not ready unless you pass `--force-deps`.
- An issue the scheduler will not start until you act carries `fact:needs-human` (next to `fact:ready`), with a comment and a `task_held` notification (`fact inbox`) saying why: its run stopped, needs you, ended with its PR open, or its PR was closed, or its runs failed 3 times in a row (`concurrency.max_failed_runs`). Remove the label, or start it with `fact run`, which removes it first and resets the failure count. A failed run below the cap is retried. A file task whose last run ended any way but failed is never restarted by the scheduler.
- It leaves part of your Claude subscription to you: while a usage window is at 90% or more (five-hour) or 95% or more (weekly), it starts no new run (`claude.reserve` in `fact.yaml`; `claude.usage_gate: false` turns this off, and it is off when `ANTHROPIC_API_KEY` is set). It reads usage from your local Claude Code login only when a task would start. Runs already going are not touched.
- `fact scheduler status` shows what the next tick would start, why the others wait, and the usage it last read with each window's reset time.
- On Windows, the worker keeps the PC from sleeping while a run is active (`worker.keep_awake`).

The scheduler ticks inside the API. In dev (`env` `local`, `test` or `dev`) it ticks every `concurrency.scheduler_interval_seconds` (30) and is on at every API start; `fact scheduler off` stops it until `fact scheduler on` or the next API start. Elsewhere there is no loop and no switch: an outside cron or webhook calls `POST /api/v1/scheduler/tick`. Check `fact scheduler status` before leaving it on: tasks whose past runs failed are retried. See `docs/ARCHITECTURE.md`.

## Workspaces

Each run works in its own git clones under `<data_dir>/workspaces/<run_id>` (`data_dir` defaults to `~/.fact`). The workspace is removed once the run's PR is merged or closed, or the run is stopped; a run that fails or needs you (including red CI on an open PR) leaves it in place, with its branch and unpushed commits, until you delete the folder. See `docs/spec/09-runners-and-harnesses.md`.

## GitHub App

fact works on GitHub as its own App (setup in `docs/spec/12-integrations.md`). Put `FACT_GITHUB_APP_ID` and `FACT_GITHUB_APP_PRIVATE_KEY_PATH` in `.env`, then check access to a repository:

```
uv run fact github check <owner/repo>
```

It prints the App, the installation and the token expiry (never the token), or a readable error. With the App configured, the worker commits as `fact[bot]`, pushes each run's branch and opens its PR as the App; without it, commits use `commit_author_name`/`commit_author_email` and opening a PR fails (see `worker/README.md`).

To set up a task repository, `uv run fact github labels sync <owner/repo>` creates the labels fact uses (`fact:*`, `type:*`, `complexity:*`) and fixes the colour and description of existing ones. It prints what changed and never deletes a label. fact picks up open issues labelled `fact:ready`; their `type:*` and `complexity:*` labels, an "Acceptance criteria" checklist and blocked-by links or `Depends on #n` lines become the task (see `docs/spec/12-integrations.md`). While a run works on an issue, fact labels it `fact:running` (and `fact:needs-human` while a gate waits for you), comments with the run and PR links, and closes the issue once the PR merges. A run that ends needing you (stopped, needs a person, PR open or closed) labels it `fact:needs-human` and comments why; a failed run says nothing until the failure cap holds the issue.

## The API

Run it alone with `uv run python -m fact.api`. It serves on the host and port from `fact.yaml`, applies DB migrations on startup and registers the projects listed there. Check it with `GET /health`; list projects with `GET /api/v1/projects`. Runs are started with `POST /api/v1/runs/start`; runs, steps and events are recorded through `/api/v1/runs`, `/api/v1/steps/{id}` and `/api/v1/runs/{id}/events`. Gates are answered with `POST /api/v1/runs/{id}/gates/{gate_id}`, instructions sent with `POST /api/v1/runs/{id}/instructions`, a run's live state read with `GET /api/v1/runs/{id}/status`, and a run stopped with `POST /api/v1/runs/{id}/stop`. Notifications are created with `POST /api/v1/notifications` (one per `dedupe_key`), listed with `GET /api/v1/notifications?unread=true` and marked read with `POST /api/v1/notifications/{id}/read`. The scheduler ticks once with `POST /api/v1/scheduler/tick`, is described with `GET /api/v1/scheduler` and, in dev only, turned on and off with `POST /api/v1/scheduler/on` and `/off`; `GET /api/v1/scheduler/preview` shows what its next tick would do. Agent steps record the Claude usage windows they see with `PUT /api/v1/usage/claude`; `GET /api/v1/usage` lists them.

A published route or field never changes: `api/openapi.json` is the committed OpenAPI schema, and a test fails when the live API removes or changes anything in it, or adds something until you regenerate it with `uv run python scripts/openapi_snapshot.py`.

Every response carries an `X-Request-ID` header (send your own to propagate it), and each request produces one `request_completed` log line. Set `FACT_LOG_LEVEL` to override the log level (default DEBUG locally, INFO in `test` and `prod`); see `docs/ARCHITECTURE.md` for logging details.

## Workflows

A project's workflows are Python classes in its `.fact/workflows/`, built on `FactWorkflow` from `fact.sdk`. fact's own `Standard` workflow (`.fact/workflows/standard.py`) prepares a workspace, runs an implement session, loops lint and test with fix sessions until green, has an agent review the change against the task (and address its findings), then runs a learn session that updates the repository's docs and writes the PR title and summary, marks the task done and opens the PR. A failed learn session does not fail the run: the PR opens with the implement session's text. It then waits for the PR's CI; a failing job's log goes to a fix session whose commit is pushed to the same PR, up to two times. Only when GitHub reports the PR as conflicting with its base branch, or refuses the merge as out of date, does the run merge the base into its branch (an agent resolves any conflicts), push and wait for CI again. When the checks or CI stay red after their fix sessions, the run does not end: it opens a gate and waits for a person, for as long as it takes and at no cost. Approving runs that loop again (extra instructions sent to the run reach its next fix sessions); rejecting ends the run needing you, with no PR for red checks and the PR open for red CI. Once CI is green, fact classifies the change set: one routed `auto_merge` (labelled `fact:auto-merge`) in a project with `auto_merge: true` is merged by fact itself with the project's `merge_method`, and the run ends `merged` without notifying you; one that needs a review (`fact:needs-review`, also when auto-merge is off or GitHub refuses the merge) notifies you that the PR is ready, with the reasons, and the run waits (shown as waiting for review) until you merge or close it, then ends `merged` or `closed`. When the run ends, however it ends (stopped and failed included), a cost comment on each PR it opened shows time, tokens and cost per step, including time spent waiting for you. You get a notification (see `fact inbox`) when a gate opens, when the PR is ready, and when the run ends.

The worker loads each registered project's workflows from the latest commit on its home repository's default branch and pins every run to that version, so a run finishes on the code it started with. Merged workflow changes take effect when the worker restarts; runs still pinned to the old version then wait, and a plain `fact stop` cannot end them because their worker is gone, so stop runs before restarting (otherwise end them with `fact stop --force` and delete their workspace folder by hand). See `.fact/workflows/README.md` and `worker/README.md`.

## Documentation

- **Design:** `docs/spec/`, one file per section, starting from `docs/spec/README.md`.
- **Architecture:** `docs/ARCHITECTURE.md`, index of components and what exists in code today.
- **Work:** GitHub issues on `Kahncode/fact`. `tasks/backlog.md` holds ideas not yet planned; `tasks/README.md` describes the task-file format for projects that use files.

