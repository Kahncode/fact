# Software factory UX: Warp Factories and Factory (research, 26 Nov 2026)

Read from public docs only (no account, no login-gated pages). Used to design fact's web UI (`docs/spec/11-ui-and-notifications.md`). Unverified: actual visuals (colors, fonts), Factory's in-app Analytics and inbox pages, keyboard shortcuts beyond Factory's changelog.

## Warp Factories (docs.warp.dev/factories)

**Hierarchy and navigation.** Team → factory → repositories, workers, automations. A factory is one definition over a set of repositories that ship together; there is no project concept below it. Team-wide sidebar: Home (personal), New Chat, Runs, MCPs and apps, Secrets, Integrations, then the list of factories. Per factory: Dashboard, Blueprint, Workers, Automations (Webhooks tab), Scorers, Self-improvement, Benchmarks, Factory definition, Settings.

**Home (the inbox).** Defaults to the filter "Waiting on you"; other filters Active, Complete, Blocked, Failed; search over runs; grouped Today / This week / All time; items grouped by work item; opening an item shows a side pane and marks it read without resolving it. Item kinds: Asked question, Answered question (info), Spec review, PR review, Blocked, Failed. Items clear on their own (a review submitted, the PR merged or closed, the question answered); Mark as resolved by hand, with Undo. Results go back to the source tool (Slack thread, Linear ticket, GitHub PR); no email or push documented.

**Run page.** Timeline, cost (including child runs, split across the PRs it produced) and child runs grouped under their parent; actions: view the worker session, stop, score, add to a benchmark. No chat input on run pages; the worker session takes instructions while the sandbox lives and becomes a transcript afterwards. Session content: prompt, plan, task list, decisions, commands, logs, terminal output, artifacts, diffs, environment. Artifact types: plan, pull request, screenshot, file, external reference.

**Blueprint.** The workflow diagram; selecting a stage shows its worker and instructions, selecting a transition shows its condition. Loops are transitions (review sends findings back to implementation; a plan gate with changes requested loops back to planning). Gates ask a question and wait for the answer.

**Factory definition as code.** `factory.yaml` plus `workers/<name>/worker.md`, `automations/<name>/automation.md`, `scorers/<name>/scorer.md` (Markdown body = prompt or rubric, YAML front matter = config), `runners/*.yaml`, `skills/*/SKILL.md`, `benchmarks/<suite>/`. Published JSON Schemas, a validator and a PR check; edits go through a branch and a PR; a run keeps the definition it started with. Definitions hosted by Warp (editable in the app), on GitHub (read-only in the app) or API-only.

**Settings per factory.** Identity (name, avatar, handle), repositories, PR authorship (worker or run creator), analysis model, runners, integrations, deletion.

**Dashboard metrics.** Total runs (by agent, status, source, model); PRs opened; PRs merged; Autonomy (merged PRs with no human code push); PR cycle time (median, per stage: kickoff → PR, first review, merge); Cost per PR (median; compute, platform, inference; by PR size S/M/L/XL at 100/500/1,000 changed lines); most expensive PRs; opened vs merged chart; run breakdown; Scorer cards; newest self-improvement PRs.

**Scorers, self-improvement, benchmarks.** Scorers: LLM judge per run (instructions, model, classifications with scores, pass threshold, sample rate). Self-improvement: follow-up runs open PRs on the factory definition, each listing the regressions it addresses. Benchmarks: suites of tasks replayed against setups (branches of the definition), with repetitions, scorers, cost, duration, and a recommendation (best, most efficient, cheapest); any finished run can be added to a suite.

## Factory (docs.factory.com)

**Hierarchy.** Organization (with sub-organizations, groups, roles Owner / Manager / member), agent profiles (service account, compute, repositories, credentials, default model), projects (a git root with `.factory/`), folders, users. Settings layer Org > Runtime > Folder > Project > User > built-in; org "hard controls" cap what lower levels may choose, other settings take the most local value.

**App.** Sidebar: New session, sessions list with filters, Code Review, Automations, Mission Control, Workstreams, Wiki, Analytics, Settings. A session shows the transcript (tool rows, a focus mode folding runs of tool calls into one summary row, subagent rows, question cards with why it asks) beside a right pane with Changes, Files, Terminal and previews (documents, PDFs, live sites, full diffs with inline comments). Workstreams group agents as "Waiting on you", "Working", "Idle".

**Code Review.** Inbox tabs are saved GitHub-syntax searches (up to 20); actions Fix (conflicts, failing checks, review comments) and Auto-fix and Auto-merge switches per PR.

**Automations.** List (title with status and alert icon, owner, trigger, runs on), detail with a 7-day success rate and an events feed (runs, alerts, deliveries); statuses Active, Paused, Degraded, Failed; automatic pauses with a reason (repeated failures, missed runs, capacity).

**Attention.** Desktop notification sounds while the app is in the background; email for automation alerts (first failure at once, then a daily digest); usage alerts at 80% and 100% to owners and managers; Slack, Linear, Jira, Teams delegations reply in the thread or ticket.

**Shortcuts.** Cmd/Ctrl+K command palette with category tabs and ranked results (sessions by title and by transcript text, actions); Cmd+F find in page; settings search matches row descriptions.

**Analytics (from the API).** Credits per day by model, user and source; sessions (count, credits, duration, primary intent, linked issues and PRs); spend by the user who triggered a run; tool calls, MCP, skills, slash commands and hooks usage; autonomy ratio (avg, p50, p90); user turns per session; DAU/WAU/MAU by client; files created and edited by language; commits and PRs created; PR size, state, open-to-merge times and attribution (sessions, credits, contributors); tickets and projects with story points; usage windows (five-hour, weekly, monthly: used percent and reset time) with alert thresholds and credit limits per org, group and user.

## What fact borrows

- An inbox that opens on "Waiting on you", grouped by day and by run, where items resolve on their own and opening marks them read (fact's notification read/resolved split).
- GitHub-like project hierarchy with settings layered org > project > user later (Factory), project-scoped tabs.
- The run page as a timeline with cost, plus the workflow graph with loops as transitions (Warp's Blueprint).
- Definition edits through PRs, runs keeping the definition they started with (already fact's pinned workflows).
- Insights ideas: see `tasks/backlog.md`, Insights.
