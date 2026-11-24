# Observability

Every step attempt writes one row with its timing, tokens and cost, plus a log file and a transcript; the UI reads these, and Temporal history is only for debugging the engine.

**How data flows**

1. A step activity emits structured events to the factory API: `step_started`, `log`, `agent_message`, `tool_call`, `cost_update`, `step_finished`.
2. The API writes rows to SQLite (`steps`, `events`) and the content to object storage at `runs/<run>/<step>/<attempt>/`: `output.log`, `transcript.jsonl` and other artifacts such as diffs. The rows keep only links to those objects.
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
| Outcome | run | merged, closed, abandoned, failed |

Logs and transcripts are kept forever for now, like rows; a retention setting can come later if storage grows.

### Capacity and monitors

Every job runs on a runner pool with its own concurrency cap. Budgets, and from v0.1 subscription usage windows, decide whether a queued job may start.

**Runner pools.** A pool is a place where sandboxes run: your PC (worktrees or local containers), GitHub-hosted runners, Daytona, and so on. Each pool is a Temporal task queue whose workers allow at most the pool's cap of sandboxes, so extra jobs wait in the queue and show as Queued. A project picks its pool; v0 has one pool, your PC.

| Cap | Scope | When reached |
| --- | --- | --- |
| Concurrent sandboxes | Per runner pool | The job waits for a free slot |
| Concurrent runs | Whole factory, and per project | New runs wait as Queued |
| Budget (USD) | Per project in USD per day (a project setting), optional per-run cap; Claude Code spend read from its own usage reporting | Alerts at 50% and 80%; at 100% new steps wait and you are notified |
| Usage window (v0.1) | Per subscription agent account, such as a Claude Code plan | New agent steps wait until the window resets; running steps finish |

**Monitors: use what already exists.**

| Limit | Provided by |
| --- | --- |
| Max turns | The harness's own flag, such as `claude -p --max-turns` |
| Max time per step | Temporal activity timeout |
| Max cost | The factory compares each step's reported cost with the budgets above |
| Stalled process | Temporal heartbeat timeout; the activity is killed and retried |

No custom progress detection in v0. A tripped limit ends the step with a typed outcome, shows on its node, and lands in the Problems list. Defaults per step kind live in `fact.yaml`.
