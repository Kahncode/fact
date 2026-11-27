# Observability

Every step attempt writes one row with its timing, tokens and cost, plus a log file and a transcript; the UI reads these, and Temporal history is only for debugging the engine.

**How data flows**

1. A step activity reports to the factory API when it starts and ends (one `steps` row per attempt: a short `name` telling apart steps that share a stage, such as the check `lint` or `test` in `checks`, or the prompt `implement` or `fix_checks`; timings, tokens, cost, outcome, outputs). Nothing else about a step goes in the database.
2. Its log and agent transcript are files in the artifact store at `runs/<run>/<step>/<attempt>/`: `output.log`, `transcript.jsonl` and other artifacts such as diffs; the row keeps only their keys. A stopped run's handoff note is stored per run, at `runs/<run>/handoff.md`.
3. **Live (decided 26 Nov 2026, M4, not built yet):** while the step runs, the worker sends new log and transcript text to the API about once a second; the API appends it to those artifact files and pushes it to open log panels over SSE. The upload at step end stays as a final reconcile. Today both files are uploaded only when the step ends.
4. Every past run stays inspectable: its stages, every step attempt, and each attempt's log, transcript and artifacts.

The `events` table and `POST /runs/{id}/events` from M0 were never used; M4 removes them (a new migration).

**What is measured**

| Measure | Per | Note |
| --- | --- | --- |
| Machine time | step attempt, run | Activity start to finish |
| Human wait time | gate, run | Gate opened to answered; kept apart from machine time |
| Queue time | step | Scheduled to started; shows a saturated worker |
| Tokens and cost (USD) | step attempt, run, project, day | From the harness only; a cost the harness did not report is unknown (NULL), never estimated |
| Turns and peak context | step attempt, run | Agent steps: turns are model calls over the whole session; peak context is the input of its largest turn (a run shows its largest step) |
| Attempts and loops | step, run | e.g. implement ran 3 times before tests passed |
| Human touches | run | Gate answers, extra instructions, manual commits; the metric Warp tracks |
| Outcome | run | Generic, whatever the workflow: succeeded, nothing_to_do (succeeded with nothing to do: no output), failed, needs_human (gave up, such as an exhausted checks loop), stopped (a person stopped the run, or rejected its plan at the `approve_plan` gate). Today's change-set outcomes (pr_opened, merged, closed, abandoned) become states of the change set output |
| Outputs | run | What the run produced, recorded by its steps: a change set and its state, tasks created, a deployment and its URL, and so on |

Logs and transcripts are kept forever for now, like rows; a retention setting can come later if storage grows.

### Capacity and monitors

Every job runs on a runner pool with its own concurrency cap. Budgets and subscription usage windows decide whether a queued job may start.

**Runner pools.** A pool is a place where runs execute: your PC (clones or local containers), GitHub-hosted runners, Daytona, and so on. Each run gets one runner from its pool, an environment holding its workspace where all its workspace steps execute, disposed of when the run ends. A pool has one cap, `max_concurrent_runs`, set where the project configures its pool (`runner_pool` in `.fact/project.yaml`); the scheduler starts no run on a full pool, so extra work shows as Queued. Task queues only route a run's steps to its runner; they cap nothing. A project picks its pool; v0 has one pool, your PC (`local`). There is no factory-wide or separate project cap.

| Cap | Scope | When reached |
| --- | --- | --- |
| Concurrent runs | Per runner pool (`runner_pool.max_concurrent_runs`, project config) | The cap counts concurrent runs only (queued or running, `05-domain-model.md`): a run waiting on a person or a usage limit holds no slot, and a run resuming from a wait queues for a slot like a start, never going over a cap. The scheduler starts no new run until a slot is free. v0: `max_concurrent_runs` (default 2) counts runs queued, running or waiting, usage-limit pauses included, as a waiting run keeps its workspace; a run started by hand with `fact run` is not held back |
| Failed runs in a row (M2-14) | Per task | The scheduler retries a task whose runs failed until `concurrency.max_failed_runs` (default 3) failed in a row, counted back to the latest run a person started; then it holds the task until `fact run` starts it by hand. On an issue (issue 73) each hold is the `fact:needs-human` label with a `task_held` notification: the run ends needs a person, stopped, PR open or closed add it, and the tick adds it once when the failure cap is reached; removing the label releases the issue (`08-planning.md`, The label is the hold). A file task whose last run ended any way but failed is never restarted by the scheduler |
| Budget (USD) | Per project in USD per day (a project setting), optional per-run cap; Claude Code spend read from its own usage reporting | Alerts at 50% and 80%; at 100% new steps wait and you are notified |
| Usage window (M2) | Per subscription agent account, such as a Claude Code plan | From M5 the runner owns this check: it stops polling while a window is inside the reserve, and the factory only sees the pool's capacity drop to zero (`19-cloud.md`). Until then: The scheduler starts no new run while a window is inside the share left to the person (`claude.reserve`: 10% of the five-hour window, 5% of the weekly ones by default), reading usage from the local Claude Code login only when a task is ready, and again only once the full window's reset time has passed; runs already going continue. Unreadable usage is logged as an error and doesn't stop starts. The gate is on with `claude.usage_gate` (default true) and off when `ANTHROPIC_API_KEY` bills the API instead (M2-14). A step that hits the limit (M2-13) ends `rate_limited` with a `resume_at`; the workflow marks the run `waiting` with the reason `usage limit until <time>` (in `fact status`), sleeps on a durable timer until the window resets, however long, then resumes the same session. These pauses don't count as resumes; after 20 in one agent step it ends `needs_human`. Every agent step records the windows it saw (`PUT /api/v1/usage/claude`, table `usage_windows`; `GET /api/v1/usage` lists them) |

**Run queue (M5.3).** Compute is handed out from one queue, a plain table in the factory database. The tick and webhooks only fill it; granting slots is immediate, on events.

- **Entries** are `start` (a ready task, upserted by the tick, removed when no longer ready) and `resume` (a run whose wait ended and needs compute again).
- **A slot covers compute, not the whole run.** A run holds a slot from its grant until it enters a wait on something outside the factory: a gate, review, CI, a usage reset. Before the wait it releases the slot; when the wait ends with work to do (a gate answered, changes requested, CI failed, `sync_base` needed) it adds a `resume` entry and waits for the `slot_granted` signal. Work that needs no compute (a merge once CI is green) takes no slot. A run started by hand queues like any other.
- **Order:** effective priority, then resumes before starts, then age. A `resume` ranks at the higher of its task's effective priority and `high`, so only urgent work goes ahead of a run already under way. Priority comes only from the tracker (`priority:*` labels); there is no manual reordering.
- **Grants:** a single dispatcher in the API, holding a Postgres advisory lock (safe with several API replicas), grants the first entry its runner pool can take: under the pool's `max_concurrent_runs` (the only cap, configured with the project's pool) and within its live capacity (the slots its connected runners advertise; zero while a runner waits out a usage window). There is no factory-wide or separate project cap. It wakes on events (a slot released, an entry added, a gate answered, a tick) and runs a slow periodic pass that re-ranks and reconciles: a slot held by a run that ended, or that Temporal no longer shows running, is freed. The checks `start_task` makes today move into the grant. Nothing is kept in process memory.
- **Workspaces outlive slots.** A waiting run keeps its workspace until the project's `workspace.keep_while_waiting` (a duration, or `forever`, the default) runs out; then it tears it down (its branch is pushed), and on resume it prepares a fresh workspace from the remote branch with a handoff note. The teardown timer is built later; the slot logic never assumes a workspace survives.
- The pool's `max_concurrent_runs` then counts concurrent runs only (it counts waiting runs too until then), and `fact scheduler status` shows concurrent and waiting runs apart.

**Seeing what the scheduler will do (M2-14).** `fact scheduler status` previews the next tick: the ready tasks in start order, and why each one waits (dependency, cap, failure cap, or a full usage window with its reset time), plus the usage the scheduler last read, each window with its reserve and reset time, or the error when usage is unreadable. The preview reuses the last answer, which the API keeps in memory (the usage gate runs in the API, on the machine whose Claude Code login it reads), and never reads usage itself. While any run is queued, running or waiting, the worker keeps a Windows PC from sleeping (`worker.keep_awake`, default true).

**Monitors: use what already exists.**

| Limit | Provided by |
| --- | --- |
| Max turns | The harness's own flag, such as `claude -p --max-turns` |
| Max time per step | Temporal activity timeout |
| Max cost | The factory compares each step's reported cost with the budgets above |
| Stalled process | Temporal heartbeat timeout; the activity is killed and retried |

No custom progress detection in v0. A tripped limit ends the step with a typed outcome, shows on its node, and lands in the Problems list. Defaults per step kind live in `fact.yaml`.
