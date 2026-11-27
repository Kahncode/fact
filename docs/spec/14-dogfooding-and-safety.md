# Dogfooding and safety

The factory's own repo is its first project, so the running factory must never be the code an agent is editing.

- The factory runs from a pinned checkout: `fact use <ref>` makes a detached worktree of the factory repo at `<data_dir>/pinned/factory/<sha>` and marks it current, and `fact dev --pinned` runs the API and worker from it. Agents only ever work in clones under `<data_dir>/workspaces/`; the runner refuses a workspace inside `pinned_dir` or the running factory checkout.
- Changes to the factory land through a normal run and its PR, merged by fact when classified `auto_merge` or by a person after review. A new version runs once a person pins it with `fact use` and `fact dev` picks it up, or once `fact dev --follow <ref>`, which the person started, upgrades to it. Runs in flight stay pinned to the version they started on and finish on it (below).
- Changes to the factory follow the same classification as any project (decided 26 Nov 2026): fact bootstraps itself, and a low-risk change to fact merges without a person. The rule that the factory's own changes always need a human merge is retired. CI is the gate: a version whose CI passed is trusted to start, with no health check of its own.

## Upgrading while it runs

Decided 27 Nov 2026. Several runs go at once and some wait on people for days, so there is never a moment with no run active: an upgrade never waits for runs to end, and stopping or starting the factory never loses a run. Every process can be thrown away at any time; what a run needs lives in Temporal, the database, the artifact store and the workspace on disk.

**Processes are tracked in files, not by parentage.** `fact dev` is a small loop that makes what runs match what should run. Each process it starts (Temporal, the API, each worker) runs detached and has a record in `<data_dir>/run/`: its role, build, pid, process start time (so a reused pid is never taken for it) and port. A restarted `fact dev` adopts the live processes it finds and starts the missing ones, so restarting `fact dev` itself (new CLI code, a crash, a reboot) touches no running work. A lock file in `data_dir` refuses a second `fact dev`, pinned or from the source tree; both share one data directory. Each process watches a stop file, so `fact dev` asks it to stop gracefully on every platform (Windows has no SIGTERM for a detached process).

**What should run:**

- Temporal, started first and stopped last.
- One API, always the newest pin. Old workers call it: the OpenAPI snapshot test already keeps every published route working for them, and every client call to the API retries while the API restarts. The scheduler's on/off state lives in the database, so an API restart keeps it.
- One worker process per *worker generation*: a factory pin with each project's home SHA at the time it started (one version `<factory SHA[:12]>+<home SHA>` per project). The newest generation is current; an older one runs while Temporal reports any of its versions `DRAINING` (runs still pinned to it) and is stopped once all are `DRAINED`. After a reboot, every generation with open runs starts again, so a run never stalls on a version no worker serves.

**`fact shutdown` stops the processes, not the runs.** It stops every worker gracefully, then the API, then Temporal, and `fact dev` exits; Ctrl-C on `fact dev` does the same. A worker's graceful stop ends a running agent step `AGENT_INTERRUPTED`, so the next start resumes its session; other activities retry once their heartbeat times out. Runs stay open in Temporal and carry on at the next `fact dev`: only the agent turn and the check in progress are redone. There is no flag to wait on and nothing to cancel.

**`fact stop <run> --force`** ends a run at once, without its stop handling: it terminates the workflow in Temporal, and the API records the run `stopped` (reason `forced`) and the task's hold, which the terminated workflow cannot. No handoff note is written and the workspace is kept; the command prints its path. A plain `fact stop` stays the graceful stop with its handoff and teardown.

**An upgrade adds a generation; it never replaces one.** When the followed ref or a project's home branch moves (`fact dev --follow`, checked every 60 s), or after a `fact use` by hand:

1. Pin the new version (`fact use`), building its checkout and environment; nothing running is touched.
2. Restart the API from the new pin; it runs the database migrations at start.
3. Start the new generation's worker next to the old ones; it makes its versions current, so new runs start on it. If it fails to start, the old generation stays current and the failure is printed and retried at the next check.

Runs already going finish on their own generation. A rollback is `fact use <older ref>`, another upgrade.

**Migrations are additive.** Two generations share one database, so a migration must keep the previous generation working: it adds tables, nullable columns and columns with a default. A drop, a rename or a new constraint on existing data is a *breaking* migration and declares `breaking = True`; a test fails on a migration that does any of these without it. This narrows "No backwards compatibility before 1.0" (`AGENTS.md`) only between neighbouring generations: stored data is still never fixed up, and a database reset stays allowed (it is a breaking upgrade). Breaking migrations should be rare, since the factory mostly adds.

**A breaking upgrade stops and restarts the open runs, with no person involved:**

1. Stop the scheduler and record the open runs and their tasks in `<data_dir>/run/upgrade.json`, so a crash part way resumes the upgrade.
2. Stop each open run gracefully (`fact stop`, reason `breaking upgrade`) on its own generation and wait until each shows `stopped`: each writes its handoff note and keeps its branch and PR.
3. Upgrade as above (the API migrates), then stop the old generations.
4. Start each recorded task again as a manual start (`fact run`, which releases the hold the stop added); each resumes from its branch with `$handoff`. Turn the scheduler back on if it was on.

**Disk cleanup is a command.** `fact cleanup` removes every pinned checkout (`pinned/factory/<sha>`, `pinned/projects/<project>/<sha>`) that is not current and whose generation has no worker running and no open run, deletes those versions from Temporal, removes stale process records and runs `git worktree prune` in the source tree. A checkout is renamed `.trash-<sha>` before it is deleted, so a half-deleted folder is never taken for a pin; one that cannot be deleted yet (a file still held on Windows) stays `.trash-` and the next `fact cleanup` tries again. It is safe to run at any time. Workspaces are not version data: runs remove their own.

**Guardrails for every project**

- Budgets per step and per run, a cap on loop iterations, and timeouts on every activity.
- Agents get only the secrets a step needs; model keys and the GitHub token stay with the worker.
- Steps that turn agent permission checks off run in the Docker runner (from v1).
- The token cannot bypass branch protection; there are no pushes to default branches.
- fact acts only on people who can already write to the repository: a review or comment from anyone else (on a public repository, any GitHub account) is ignored, so it can neither merge a PR nor steer an agent (`07-reference-workflow.md`, Only maintainers count).
