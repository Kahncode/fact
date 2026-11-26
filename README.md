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

`fact dev` starts the Temporal dev server (UI at http://localhost:8233), the API and the worker, prefixing each output line with the process name. Ctrl-C stops all three; if one exits, the others are stopped too. In another terminal, `uv run fact projects list` shows the projects the API knows about.

## Letting fact work on itself

fact's own repository is its first project, so the running factory must not be the checkout an agent edits. Pin a version and run from it:

```
uv run fact use main
uv run fact dev --pinned
```

`fact use <ref>` checks the ref out (from `origin` when it exists there) as a separate copy under `<data_dir>/pinned/factory/<sha>` and installs its dependencies; `fact dev --pinned` runs the API and worker from that copy while still reading your `fact.yaml` and `.env`. To pick up merged changes, run `fact use main` again and restart `fact dev --pinned`, but only once runs waiting for review have ended: a run still waiting when the worker restarts on new code stalls (after merging a run's PR, wait until `fact status` shows it `merged`, within about a minute). See `docs/spec/14-dogfooding-and-safety.md` and `docs/ARCHITECTURE.md`.

## Starting a run

With `fact dev` running, start the project's workflow for a task and follow it:

```
uv run fact run task:M1-02
uv run fact runs
uv run fact status run-task-M1-02-1
```

`fact run` reads the task from the project's default branch on `origin` (not your working tree), refuses if its dependencies are not done (`--force-deps` starts anyway), and prints the run id, workflow id and a link to the run in the Temporal UI. Pass `--project` when several projects hold the same task key. `fact status` accepts a run id or a workflow id and shows each step with its duration, outcome, cost and log links, plus the gate the run is waiting at, if any.

When a run waits for you, answer its gate, or send instructions to its next agent session (a running session is not interrupted):

```
uv run fact gate run-task-M1-02-1 approve
uv run fact gate run-task-M1-02-1 reject --note "wrong approach, see the task"
uv run fact steer run-task-M1-02-1 "Keep the old function name"
```

`fact gate` answers the open gate; pass `--gate <id>` to answer a specific one.

A project can require you to approve each task's plan before work starts: set `require_plan_approval` in its `.fact/project.yaml` (default true), or in a task's front matter to override it. The run then waits at an `approve_plan` gate; rejecting it ends the run `dropped`. fact's own project turns this off, since marking a task `status: ready` already approves it.

To see what needs you, check your inbox. A run notifies you when it waits at a gate, when its PRs are ready for review, and when it fails or gives up:

```
uv run fact inbox
uv run fact inbox --all
uv run fact inbox read 3
```

`fact inbox` lists unread notifications, newest first, with a link to the PR or the run; `--all` includes those already read, and `fact inbox read <id>` marks one read. The web UI's Notifications page replaces this in M3.

Each run works in its own git worktrees under `<data_dir>/workspaces/<run_id>` (`data_dir` defaults to `~/.fact`). The workspace is removed once the run's PR is merged or closed; a run that fails or needs you (including red CI on an open PR) leaves it in place, with its branch and unpushed commits, until you delete the folder. See `docs/spec/09-runners-and-harnesses.md`.

## GitHub App

fact works on GitHub as its own App (setup in `docs/spec/12-integrations.md`). Put `FACT_GITHUB_APP_ID` and `FACT_GITHUB_APP_PRIVATE_KEY_PATH` in `.env`, then check access to a repository:

```
uv run fact github check <owner/repo>
```

It prints the App, the installation and the token expiry (never the token), or a readable error. With the App configured, the worker commits as `fact[bot]`, pushes each run's branch and opens its PR as the App; without it, commits use `commit_author_name`/`commit_author_email` and opening a PR fails (see `worker/README.md`).

To set up a task repository, `uv run fact github labels sync <owner/repo>` creates the labels fact uses (`fact:*`, `type:*`, `complexity:*`) and fixes the colour and description of existing ones. It prints what changed and never deletes a label.

## The API

Run it alone with `uv run python -m fact.api`. It serves on the host and port from `fact.yaml`, applies DB migrations on startup and registers the projects listed there. Check it with `GET /health`; list projects with `GET /api/v1/projects`. Runs are started with `POST /api/v1/runs/start`; runs, steps and events are recorded through `/api/v1/runs`, `/api/v1/steps/{id}` and `/api/v1/runs/{id}/events`. Gates are answered with `POST /api/v1/runs/{id}/gates/{gate_id}`, instructions sent with `POST /api/v1/runs/{id}/instructions`, and a run's live state read with `GET /api/v1/runs/{id}/status`. Notifications are created with `POST /api/v1/notifications` (one per `dedupe_key`), listed with `GET /api/v1/notifications?unread=true` and marked read with `POST /api/v1/notifications/{id}/read`.

Every response carries an `X-Request-ID` header (send your own to propagate it), and each request produces one `request_completed` log line. Set `FACT_LOG_LEVEL` to override the log level (default DEBUG locally, INFO in `test` and `prod`); see `docs/ARCHITECTURE.md` for logging details.

## Workflows

A project's workflows are Python classes in its `.fact/workflows/`, built on `FactWorkflow` from `fact.sdk`. fact's own `Standard` workflow (`.fact/workflows/standard.py`) prepares a workspace, runs an implement session, loops lint and test with fix sessions until green, has an agent review the change against the task (and address its findings), then runs a learn session that updates the repository's docs and writes the PR title and summary, marks the task done and opens the PR. A failed learn session does not fail the run: the PR opens with the implement session's text. It then waits for the PR's CI; a failing job's log goes to a fix session whose commit is pushed to the same PR, up to two times. When the checks or CI stay red after their fix sessions, the run does not end: it opens a gate and waits for a person, for as long as it takes and at no cost. Approving runs that loop again (extra instructions sent to the run reach its next fix sessions); rejecting ends the run needing you, with no PR for red checks and the PR open for red CI. Once CI is green, you are notified that the PR is ready, and the run stays open (shown as waiting for review) until you merge or close the PR on GitHub; it then ends `merged` or `closed`. A cost comment on the PR shows time and cost per step, including time spent waiting for you. You get a notification (see `fact inbox`) when a gate opens, when the PR is ready, and when the run ends.

The worker loads each registered project's workflows from the latest commit on its home repository's default branch and pins every run to that version, so a run finishes on the code it started with. Merged workflow changes take effect when the worker restarts; runs still pinned to the old version then wait until you drop and restart them. See `.fact/workflows/README.md` and `worker/README.md`.

## Documentation

- **Design:** `docs/spec/`, one file per section, starting from `docs/spec/README.md`.
- **Architecture:** `docs/ARCHITECTURE.md`, index of components and what exists in code today.
- **Work:** `tasks/`, one markdown file per task grouped by milestone. `tasks/backlog.md` holds ideas not yet planned.

