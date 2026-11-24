---
id: M0-09
title: "Temporal worker skeleton"
type: feature
complexity: standard
status: planned
depends_on: [M0-03, M0-04]
human_actions: []
---

## Goal

`fact.worker` connects to Temporal and runs a trivial workflow and activity on the `control` task queue, proving temporalio works on Python 3.15 on every CI platform.

## Read first

- `CLAUDE.md`
- `docs/spec/03-temporal.md`
- `docs/spec/09-runners-and-harnesses.md` (the Execution zones table)

## Do

- Add `temporalio` to `worker` (and to `sdk`, which will hold workflow helpers).
- `fact/worker/client.py`, with `connect(settings)` using the address and namespace from settings and the Pydantic data converter (`temporalio.contrib.pydantic`).
- `fact/worker/ping.py`: a `PingWorkflow` that calls a `ping` activity and returns its result.
- `fact/worker/__main__.py`, so that `python -m fact.worker` configures logging, connects, and runs a worker on task queue `control` with the ping workflow and activity until interrupted (clean shutdown on Ctrl-C, Windows included).
- Test: `WorkflowEnvironment.start_time_skipping()` (or `start_local()`), run `PingWorkflow` through a worker, and assert the result. It must pass on the Windows CI job too.

## Out of scope

- Real activities, sandboxes and per-project workflow queues (M1).

## Acceptance criteria

- [ ] `uv run pytest worker` passes locally and on both CI jobs.
- [ ] With `temporal server start-dev` running, `uv run python -m fact.worker` starts, and `temporal workflow execute --type PingWorkflow --task-queue control` returns the ping result.
