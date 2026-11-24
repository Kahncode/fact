# UI and notifications

The factory has its own UI, built around runs, steps, PRs and cost. Temporal's Web UI stays one click away, for debugging the engine.

**What Temporal's Web UI gives us, and what it does not**

- It gives: a timeline, a compact view that groups activities, signals and timers, the full event history, pending activities, a live event feed, and downloadable history. It ships with the dev server.
- It does not show factory concepts: cost, agent transcripts, logs, PR and CI state, a stage graph of the workflow, or anything across several runs.
- So every run page has an "Open in Temporal" link, and nothing else depends on Temporal's UI.

**Views**

| View | Shows | Main actions |
| --- | --- | --- |
| Inbox | Everything waiting on you: gates, PRs to review, failed or paused runs, and the Problems list (tripped monitors, stalled runs, gates waiting too long) | Approve, reject with a note, open PR |
| Runs | All runs by status, project and issue, with cost and duration | Filter, start a run |
| Run detail | Node graph of the main steps (see Run graph below); step timeline with machine and wait time; per-step logs, transcript, cost; issue and PR links | Retry step, cancel, pause, send an instruction to the next agent step, drop and restart on the latest version |
| Projects | Repos, commands, workflow, runner, budgets, recent runs | Edit config (writes YAML) |
| Workflows | Manifest graph, current worker version, runs per version, success rate | Open its code; see which versions are still running |
| Costs | Spend by day, project, workflow and step kind; human touches per run | — |

**Run graph.** The run page shows the workflow's main steps as a left-to-right node graph, like a GitHub Actions run, drawn from the workflow's recorded stages. Each node shows its status (queued, running, passed, failed, skipped, waiting on you) and its duration. Loops show as an attempt count on the node, not as extra nodes. Clicking a node opens a side panel:

| Panel section | Contents |
| --- | --- |
| Status | State, attempt n of max, started, duration, human wait |
| Logs | Live log, agent transcript, download |
| Cost | Tokens and USD for this node, per attempt |
| Links | Grouped by repository: branch, PR, CI run; plus the issue and any preview URL |
| Actions | Retry, skip, answer the gate, open in Temporal |

A run that touches three repositories shows three PRs on its Open PRs node and three CI runs on its Wait for CI node, each labelled with its repository.

**PR review stays in GitHub.** The run detail and inbox show each PR's state, CI and review status, and link straight to it.

**Notifications**

- A `notify` step plus rules in `fact.yaml` decide what is sent.
- Events: gate waiting, PRs ready for review, run failed or needs help, budget exceeded, run finished.
- Every time a run enters a state that waits for a human, a notification is created. v0 channel: a Notifications page in the web app listing all of your notifications, read and unread, plus desktop notifications while the app is open. Later: reminders based on each user's preferences, then Slack and Discord.
- Repeats are grouped (one message per run state change) and quiet hours are respected.

**Frontend tech**: a React single-page app (Vite, TanStack Query and Router), built to static files and served by FastAPI, with SSE for live updates. Next.js is not needed: there is one API (FastAPI), no SEO, and no server rendering for a local dashboard. Revisit if the factory gets public or hosted pages.
