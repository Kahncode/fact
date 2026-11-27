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
| Attempts and loops | step, run | e.g. implement ran 3 times before tests passed |
| Human touches | run | Gate answers, extra instructions, manual commits; the metric Warp tracks |
| Outcome | run | pr_opened (the workflow ended with its PRs open, without waiting for a merge; `Standard` waits since M2-05), needs_human (gave up, such as an exhausted checks loop), no_changes (succeeded with nothing to change: no PR, issue closed; not built yet), merged, closed, abandoned, failed, stopped (a person stopped the run with `fact stop`, or rejected its plan at the `approve_plan` gate) |

Logs and transcripts are kept forever for now, like rows; a retention setting can come later if storage grows.

### Capacity and monitors

Every job runs on a runner pool with its own concurrency cap. Budgets and subscription usage windows decide whether a queued job may start.

**Runner pools.** A pool is a place where sandboxes run: your PC (worktrees or local containers), GitHub-hosted runners, Daytona, and so on. Each pool is a Temporal task queue whose workers allow at most the pool's cap of sandboxes, so extra jobs wait in the queue and show as Queued. A project picks its pool; v0 has one pool, your PC.

| Cap | Scope | When reached |
| --- | --- | --- |
| Concurrent sandboxes | Per runner pool | The job waits for a free slot |
| Concurrent runs | Whole factory, and per project | The scheduler starts no new run until one ends. v0 (M2-07): `concurrency.max_runs` (default 2) and a project's `max_runs` (default 1) count runs queued, running or waiting, usage-limit pauses included; a run started by hand with `fact run` is not held back |
| Failed runs in a row (M2-14) | Per task | The scheduler retries a task whose runs failed until `concurrency.max_failed_runs` (default 3) failed in a row, counted back to the latest run a person started; then it holds the task until `fact run` starts it by hand. A task whose last run ended any other way (needs a person, stopped, PR open, merged or closed) is never restarted by the scheduler |
| Budget (USD) | Per project in USD per day (a project setting), optional per-run cap; Claude Code spend read from its own usage reporting | Alerts at 50% and 80%; at 100% new steps wait and you are notified |
| Usage window (M2) | Per subscription agent account, such as a Claude Code plan | The scheduler starts no new run while a window is inside the share left to the person (`claude.reserve`: 10% of the five-hour window, 5% of the weekly ones by default), reading usage from the local Claude Code login only when a task is ready, and again only once the full window's reset time has passed; runs already going continue. Unreadable usage is logged as an error and doesn't stop starts. The gate is on with `claude.usage_gate` (default true) and off when `ANTHROPIC_API_KEY` bills the API instead (M2-14). A step that hits the limit (M2-13) ends `rate_limited` with a `resume_at`; the workflow marks the run `waiting` with the reason `usage limit until <time>` (in `fact status`), sleeps on a durable timer until the window resets, however long, then resumes the same session. These pauses don't count as resumes; after 20 in one agent step it ends `needs_human`. Every agent step records the windows it saw (`PUT /api/v1/usage/claude`, table `usage_windows`; `GET /api/v1/usage` lists them) |

**Seeing what the scheduler will do (M2-14).** `fact scheduler status` previews the next tick: the ready tasks in start order, and why each one waits (dependency, cap, failure cap, or a full usage window with its reset time), plus the usage the scheduler last read, each window with its reserve and reset time, or the error when usage is unreadable. The preview reuses the last answer, which the API keeps in memory (the usage gate runs in the API, on the machine whose Claude Code login it reads), and never reads usage itself. While any run is queued, running or waiting, the worker keeps a Windows PC from sleeping (`worker.keep_awake`, default true).

**Monitors: use what already exists.**

| Limit | Provided by |
| --- | --- |
| Max turns | The harness's own flag, such as `claude -p --max-turns` |
| Max time per step | Temporal activity timeout |
| Max cost | The factory compares each step's reported cost with the budgets above |
| Stalled process | Temporal heartbeat timeout; the activity is killed and retried |

No custom progress detection in v0. A tripped limit ends the step with a typed outcome, shows on its node, and lands in the Problems list. Defaults per step kind live in `fact.yaml`.
