# Observability

Every step attempt writes one row with its timing, tokens and cost, plus a log file and a transcript; the UI reads these, and Temporal history is only for debugging the engine.

**How data flows**

1. A step activity emits structured events to the factory API: `step_started`, `log`, `agent_message`, `tool_call`, `cost_update`, `step_finished`.
2. The API writes rows to SQLite (`steps`, `events`) and the content to object storage at `runs/<run>/<step>/<attempt>/`: `output.log`, `transcript.jsonl` and other artifacts such as diffs. The rows keep only links to those objects. A dropped run's handoff note is stored per run, at `runs/<run>/handoff.md`.
3. The UI subscribes over SSE and shows logs live; finished files are served as downloads.

**What is measured**

| Measure | Per | Note |
| --- | --- | --- |
| Machine time | step attempt, run | Activity start to finish |
| Human wait time | gate, run | Gate opened to answered; kept apart from machine time |
| Queue time | step | Scheduled to started; shows a saturated worker |
| Tokens and cost (USD) | step attempt, run, project, day | From the harness; priced from a table when missing |
| Attempts and loops | step, run | e.g. implement ran 3 times before tests passed |
| Human touches | run | Gate answers, extra instructions, manual commits; the metric Warp tracks |
| Outcome | run | pr_opened (the workflow ended with its PRs open, without waiting for a merge; `Standard` waits since M2-05), needs_human (gave up, such as an exhausted checks loop), no_changes (succeeded with nothing to change: no PR, issue closed; not built yet), merged, closed, abandoned, failed, dropped (a person dropped the run with `fact drop`, or rejected its plan at the `approve_plan` gate) |

Logs and transcripts are kept forever for now, like rows; a retention setting can come later if storage grows.

### Capacity and monitors

Every job runs on a runner pool with its own concurrency cap. Budgets and subscription usage windows decide whether a queued job may start.

**Runner pools.** A pool is a place where sandboxes run: your PC (worktrees or local containers), GitHub-hosted runners, Daytona, and so on. Each pool is a Temporal task queue whose workers allow at most the pool's cap of sandboxes, so extra jobs wait in the queue and show as Queued. A project picks its pool; v0 has one pool, your PC.

| Cap | Scope | When reached |
| --- | --- | --- |
| Concurrent sandboxes | Per runner pool | The job waits for a free slot |
| Concurrent runs | Whole factory, and per project | The scheduler starts no new run until one ends. v0 (M2-07): `concurrency.max_runs` (default 2) and a project's `max_runs` (default 1) count runs queued, running or waiting; a run started by hand with `fact run` is not held back |
| Budget (USD) | Per project in USD per day (a project setting), optional per-run cap; Claude Code spend read from its own usage reporting | Alerts at 50% and 80%; at 100% new steps wait and you are notified |
| Usage window (M2) | Per subscription agent account, such as a Claude Code plan | The scheduler starts no new run while a window is inside the share left to the person (`claude.reserve`: 10% of the five-hour window, 5% of the weekly ones by default), reading usage from the local Claude Code login only when a task is ready, and again only once the full window's reset time has passed; runs already going continue. Unreadable usage is logged as an error and doesn't stop starts (M2-14). A step that hits the limit (M2-13) ends `rate_limited` with a `resume_at`; the workflow marks the run `waiting` with the reason `usage limit until <time>` (in `fact status`), sleeps on a durable timer until the window resets, however long, then resumes the same session. These pauses don't count as resumes; after 20 in one agent step it ends `needs_human`. Every agent step records the windows it saw (`PUT /api/v1/usage/claude`, table `usage_windows`; `GET /api/v1/usage` lists them) |

**Monitors: use what already exists.**

| Limit | Provided by |
| --- | --- |
| Max turns | The harness's own flag, such as `claude -p --max-turns` |
| Max time per step | Temporal activity timeout |
| Max cost | The factory compares each step's reported cost with the budgets above |
| Stalled process | Temporal heartbeat timeout; the activity is killed and retried |

No custom progress detection in v0. A tripped limit ends the step with a typed outcome, shows on its node, and lands in the Problems list. Defaults per step kind live in `fact.yaml`.
