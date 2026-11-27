# UI and notifications

The factory has its own UI, built around runs, steps, PRs and cost. Temporal's Web UI stays one click away, for debugging the engine.

**What Temporal's Web UI gives us, and what it does not**

- It gives: a timeline, a compact view that groups activities, signals and timers, the full event history, pending activities, a live event feed, and downloadable history. It ships with the dev server.
- It does not show factory concepts: cost, agent transcripts, logs, PR and CI state, a stage graph of the workflow, or anything across several runs.
- So every run page has an "Open in Temporal" link, and nothing else depends on Temporal's UI.

**Views** (the first sketch; the decided pages are under "Pages, refined" below, which wins where they differ)

| View | Shows | Main actions |
| --- | --- | --- |
| Inbox | Everything waiting on you: gates, PRs to review, failed or paused runs, and the Problems list (tripped monitors, stalled runs, gates waiting too long) | Approve, reject with a note, open PR |
| Runs | All runs by status, project and issue, with cost and duration | Filter, start a run |
| Run detail | Node graph of the main steps (see Run graph below); step timeline with machine and wait time; per-step logs, transcript, cost; issue and PR links | Retry step, cancel, pause, send an instruction to the next agent step, stop and restart on the latest version |
| Projects | Repos, commands, workflow, runner, budgets, recent runs | Edit config (writes YAML) |
| Workflows | Manifest graph, current worker version, runs per version, success rate | Open its code; see which versions are still running |
| Costs | Spend by day, project, workflow and step kind; human touches per run | — |

**Run graph.** (Decided 26 Nov 2026: stages as nodes with loops as back-arrows and an attempt count, a timeline of every step attempt below, a side panel with one tab per attempt.) The run page shows the workflow's main steps as a left-to-right node graph, like a GitHub Actions run, drawn from the workflow's recorded stages. Each node shows its status (queued, running, passed, failed, skipped, waiting on you) and its duration. Loops show as an attempt count on the node, not as extra nodes. Clicking a node opens a side panel:

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
- **What notifies (decided 26 Nov 2026, not built yet).** Only what needs a person or is worth checking; a factory processes many tasks, so routine progress never notifies:
  - `gate_waiting`: always (warning).
  - `prs_ready`: only when the change set needs a human review, as decided by the classify step (`06-workflow-authoring.md`); until classify exists, every change set needs one.
  - `run_failed`: ended `failed` or `needs_human` (error).
  - `run_finished`: only for a run a person started by hand (UI, API or CLI), never for scheduler runs (info); a run ending `no_changes` always notifies, as a warning.
  - `task_held` (decided 27 Nov 2026): a run end left its task held with `fact:needs-human`, so nothing will start it until a person acts; says why (stopped, needs a person, PR open or closed, failure cap) and how to release it: remove the label or `fact run` (warning).
  - `usage_wait`: a run paused on the Claude usage limit, "waiting for Claude usage limit until <time>" (info, for now).
- **Inbox:** one page, the unresolved notifications (`05-domain-model.md`), with a history filter for all of them; read and resolved are separate.
- Every time a run enters a state that waits for a human, a notification is created. v0 channel: a Notifications page in the web app listing all of your notifications, read and unread, plus desktop notifications while the app is open (in M4 every notification fires one; filtering by severity or rules comes later). Later: reminders based on each user's preferences, then Slack and Discord.
- Repeats are grouped (one message per run state change) and quiet hours are respected.
- Each notification has a severity, `info`, `warning` or `error` (decided 26 Nov 2026, not built yet), so the inbox can sort and filter on it. Defaults per kind: `run_failed` error, `gate_waiting` warning, `prs_ready` and `run_finished` info; a run that ends `no_changes` sends `run_finished` as a warning.
- **v0 today (M2-03):** a workflow notifies through `FactWorkflow.notify` (the `notify` control activity, best effort: a failed notification never fails a run). Kinds: `gate_waiting` (each gate that opens), `prs_ready` (`notify_prs_ready`; `Standard` sends it once CI is green and the change set needs a review (routed `human_review`, auto-merge off, or the merge refused), with the classification's reasons, before waiting for the PRs to merge or close; a change set fact merges sends none; a run ending `pr_opened` sends it at the end), `run_failed` (ended `failed` or `needs_human`), `run_finished` (`merged`, `closed`, `stopped` and other ends; since M2-06 a run stopped with `fact stop` sends it too) and, since issue 73, `task_held` (`FactWorkflow.report_end` on a `stopped`, `needs_human`, `pr_opened` or `closed` end, key `<run id>:held`; the scheduler's tick records it on the last failed run when an issue reaches the failure cap). The wait for review is not a gate, so it sends no `gate_waiting`. Each notification has a dedupe key `<run id>:<state>` (such as `<run id>:gate:<gate id>`), and the API keeps one per key, so retries and replays do not repeat it. They are stored in the factory DB and read with `fact inbox` (`--all` for read ones too; `fact inbox read <id>` marks one read) until the M4 Inbox. No rules in `fact.yaml`, desktop notifications or quiet hours yet.

**Frontend tech**: a React single-page app (Vite, TanStack Query and Router), built to static files and served by FastAPI, with SSE for live updates. Next.js is not needed: there is one API (FastAPI), no SEO, and no server rendering for a local dashboard. Revisit if the factory gets public or hosted pages.

**Serving (decided 26 Nov 2026, M4, not built yet).** fact is primarily a web app. `fact dev` always starts Vite's dev server next to the API (no flag to turn it off), proxying `/api` to FastAPI. A pinned factory serves a built bundle: `fact use` runs `pnpm install --frozen-lockfile` and `pnpm build` in the pinned checkout, and FastAPI serves `web/dist`; `dist` is never committed. Node and pnpm become prerequisites. The frontend only talks to the API over HTTP, so it can later be hosted on its own, pointed at a FastAPI backend.

**The UI is the product (26 Nov 2026).** fact is a web app: everything a person does (start a run, answer a gate, send an instruction, stop a run, the scheduler, projects, notifications) happens in the UI over the API. The CLI was a placeholder until the UI existed; in the long run a person runs only `fact dev` and `fact use`.

**Look and feel (26 Nov 2026).** Light, warm and personal, not enterprise: a calm, slightly hippie feel with hand-drawn cyberpunk touches, in the spirit of the logo (a pencil-sketched cyborg profile on a sage disc). Not overdone: data stays crisp and readable. Palette from the logo: warm paper background `#F0ECE1`, sage accent `#748D78`, graphite ink for text and lines.

Style picked from the prototype (26 Nov 2026, board D of the style exploration): Fraunces for headings, DM Sans for text, JetBrains Mono for keys, ids and numbers (bold mono numbers contrast with the serif); 6 px corner radius; hand-drawn touches only on UI elements such as the circled active tab, the inbox badge and heading underlines, never on the workflow graph, whose lines stay clean. Not nerdy: no bracketed captions or terminal styling. UX references: other software factories (Warp Factories, Factory), Linear for density and keyboard use, GitHub Actions for the run graph.

**Navigation (26 Nov 2026), GitHub-like.** A project is the first level of the hierarchy, like a GitHub repository: most things live under it, and inside a project its tabs (Home, Inbox, Tasks, Runs, Insights) come first while its settings sit a little apart (as GitHub's Settings tab). Settings will layer like GitHub's and Factory's: organization, project and user (organizations and users do not exist yet). The inbox is global, shown like GitHub's pull requests or notifications entry with a count badge. Search sits in the header with a keyboard shortcut; the UI has keyboard shortcuts throughout. There is no scheduler switch (`16-decisions.md`); pausing a project is a project setting. The Claude usage meter lives in Insights, with everything about cost and usage.

**Editing config (26 Nov 2026), like Warp.** Project config lives in git. The UI shows it and can edit it: an edit commits to a branch and opens a PR, never writes the default branch. Every settings page also has a button that opens the file on GitHub to edit it there. If the editor is postponed, the page shows the config read-only with that button.

**GitHub first.** Early on, fact leans on GitHub's own UI: code changes are reviewed in the PR (run detail links each PR with its state, it has no diff viewer), and a page fact has not built yet (such as parts of Tasks) links to the matching GitHub view.

**Pages and URLs (26 Nov 2026).** Home at `/` (every project); `/projects`; a project at `/<project>`, whose default page is its Home, with tabs Home, Inbox, Tasks, Runs (`runs/<id>`), Insights (per project and per workflow) and Settings (project and workflows); factory settings at `/settings`; runs across projects at `/runs`. URLs may change as the pages are built. The Inbox opens on "Waiting on you", groups items by run and then by day (Today, This week, Older); opening an item shows a side pane with its action inline (approve or reject with a note, open the PR, open the run) and marks it read; items resolve on their own, or by hand with Undo. One search-and-command box (header, `/` or ⌘K) finds projects, tasks and runs and lists actions; Linear-style shortcuts: `g i`, `g r`, `g t` to navigate, `j`/`k` and Enter in lists, `a` approve, `x` reject, `e` resolve in the inbox, `?` for help.

**Pages, refined (26 Nov 2026).**
- **Inbox**: its own tab. The inbox is one view filtered by scope: a project's tab, a person's notifications, everything across projects (later organizations) are the same view with different filters. Its filter buttons show a count each; each row has chips linking its PR, issue and repository.
- **Home** (the global `/`, and a project's default page): a combination of widgets from the other tabs. First the inbox widget, then the top insights and a chart of runs (or tasks done) over time; lower down, the runs in progress and what is up next. A factory that works well needs no look at single tasks, so Home leads with what needs you and how the factory is doing. A project's home shows banners for states that hold its runs: paused, or "waiting for the Claude usage limit until <time>".
- **Tasks**: the tracker owns tasks, so fact shows a minimal list with filters (priority among them, shown on each row, effective priority beside it when higher) and sends people to the tracker for everything else, reacting to tracker changes (webhooks once hosted). Actions that matter at fact's level: **Run now** (when the task is ready and its dependencies are done) and **Mark ready**, on each row and on the task's detail.
- **Runs**: every run (in progress first), one row per run, filters for status, outcome, started by, workflow and date; the API pages through results.
- **Step output**: every step attempt has a log; an agent step also has its transcript. Between the agent's messages, consecutive tool activity folds into one expandable line ("Worked 7 s · ran 2 commands · read 3 files"). A subagent is one line (name, task, status, duration) that opens its transcript in a side pane beside the parent's, with a breadcrumb for deeper levels, so the hierarchy (which agent started which) stays visible.
- **Run detail**: header with task, status, why it waits, PRs, started by and trigger, workflow and worker build, started and ended, total time (machine plus human wait), total cost, run id with a copy button (harness and model are per step, in the step's side panel); actions Stop (with a reason), Add instruction (text queued for the run's next agent step, shown until a step takes it) and Open in Temporal; the graph, the timeline and the side panel. The timeline has one row per stage in graph order (rows match the graph's nodes); each attempt is a bar on its stage's row, so loops show as repeated bars; waits on a person or a review are hatched; long waits (days in `wait_for_review`) are drawn shortened with a break mark; hovering shows stage, attempt, times and outcome, clicking opens the side panel. No per-agent lanes and no zoom slider for now; the handoff note of a stopped run; the classification; earlier runs of the task.
- **Workflows have no tab of their own** (26 Nov 2026): authoring and configuration (which task types and complexities use a workflow, harness and model per stage, prompts, retry limits and other parameters, all in git and edited through PRs) live in **Settings**, in a Workflows section; workflow insights (the stage graph with cost and time per stage, attempts, failure rate, and the builds that ran it) live in **Insights**, as a per-workflow view.
- **Project settings**: project-wide parameters, plus the Workflows section above: task tracker integration, repositories, default harness and model, agent accounts (such as the Claude subscription), `max_runs`, plan approval, auto-merge and merge method, classify paths, Pause project, GitHub access check and label sync.
- **Task detail**: title and key linked to the tracker, body and acceptance criteria (read-only), priority (and effective priority with the task it comes from, when higher), labels, dependencies and what they block, parent and sub-issues, every run of the task; actions Run now and Mark ready.
- **Projects and New project**: `/projects` lists projects; New project onboards one: pick a GitHub repository the App can reach, check access and `.fact/project.yaml`, set the first basic configuration.
- **No factory settings page**: the developer who launches fact runs it; the UI shows only fact's version (later, when deployed).
- **New task** is the header's main action, from the planner (M6); until then there is no header action.

**Insights (26 Nov 2026).** Start small: cost, time, failures and human blockers: the share of runs that needed any human intervention (apart from the share of PRs auto-merged vs reviewed), cost and time per stage, retries, where runs fail. Human wait time and usage windows are not shown. Insights exist per workflow (how its stages behave across runs), per project, and later per organization. More ideas from Warp and Factory are in `tasks/backlog.md`.

**Desktop first, usable on a phone.** The desktop is the control plane; on a phone, everything that answers or unblocks (Inbox, gate actions, run header, stop, add instruction) and basic controls work; graph, timeline, transcripts and Insights may scroll sideways.

**Shared patterns.** One loading, empty and error state pattern across pages (empty states use the hand-drawn touch); relative times with the exact time on hover, in the local time zone; USD with 2 decimals.

**Dark mode** comes with user accounts, as a user preference. **TypeScript** is strict everywhere; API types are generated from FastAPI's OpenAPI schema.

**Auth:** none in M4. The API binds to localhost; exposing it further needs auth first.
