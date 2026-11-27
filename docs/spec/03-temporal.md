# Decision: Temporal as the engine

Use Temporal with the Python SDK. Workflows are Python code, one or more per project. Every agent call, shell command and API call is an activity.

**Why it fits**

- Durable state, retries, timeouts, timers and waiting for a human (signals and updates) come built in, so loops like "implement until tests pass, max 3" are ordinary code.
- Code, agents and persistent data mix naturally: a workflow is a Python function, and activities can do anything.
- It runs locally as one process: `temporal server start-dev --db-filename temporal.db` persists to SQLite and serves the Web UI on port 8233.
- Temporal's docs list a plugin for driving Claude Code agents from workflows through the Claude Agent SDK, so the ecosystem already covers our main harness.

**Trade-offs and how we handle them**

| Risk | What it means for us | Mitigation |
| --- | --- | --- |
| Determinism | Workflow code is replayed, so it cannot do I/O, read the clock or use randomness directly | Keep workflow code thin; all side effects live in activities; replay tests in CI |
| Constant change vs runs in flight | Changing a workflow can break runs that started on the old code | Pinned only: a run always finishes on the version it started on, with no upgrades in flight. To move a run to new code, stop it and restart it. Old worker versions keep running until their runs end. |
| History size limits | 2 MB per payload, 50 MB or 51,200 events per run history | Never put logs, transcripts or diffs in payloads; store them in object storage, keep only their links in the factory DB, and pass those links. Long planning sessions use continue-as-new |
| One more process to run | Temporal server beside the API and worker | One `dev` command starts all three |

**Alternatives considered**: Restate, DBOS, Hatchet and Inngest are lighter options. DBOS is worth remembering because it keeps state in Postgres, which would sit well with Supabase. We pick Temporal for maturity, its Python SDK, its Web UI and the Claude Agent SDK integration.

**Internal conventions (v0 decisions)**

- Gates are Temporal **updates**: the UI or the MCP server sends approve or reject with a note, and gets an error back if the gate is no longer open.
- Steering is a **signal**: "add this instruction" is queued and added to the next agent attempt's prompt, never interrupting a running one.
- Status is a **query**: current stage, attempt and pending gate, for the UI and the MCP server.
- Every step ends with a typed outcome: `passed`, `failed_checks`, `failed_error`, `needs_human`, `over_budget`, `stopped_by_monitor`, `stopped` or `rate_limited` (an agent step the usage limit refused; `FactWorkflow.agent` waits for the reset and resumes it, so a workflow never sees it). Workflows branch on these outcomes, never on parsed agent text.
- Every run has one `run_id` carried on every log line, event and telemetry record.
- Temporal runs only project workflows. Picking up work (the scheduler's tick, the only background job in fact) is not a Temporal workflow: the tick is stateless and idempotent, and every start is re-checked by `POST /runs/start`, so durability buys it nothing, while a workflow per tick would write history (and fill the Temporal UI) every 30 s even with nothing ready. The tick is one function in the API, `run_tick()`, called by `POST /api/v1/scheduler/tick`, by GitHub webhooks once hosted, and by a loop in the API process (every 30 s in dev; hosted, the same loop with one API replica, until the M5.3 dispatcher's advisory lock lets it run on several). It touches Temporal only to start a project workflow.
