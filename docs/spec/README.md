# fact v0 spec

This folder is the source of truth for fact's design. It was moved here from the "Software Factory — v0 Spec" Claude Doc on 24 Nov 2026. Agents keep it current: when a change alters a decision described here, the same commit updates the section.

Read only the sections a task points you to.

| File | Covers |
| --- | --- |
| [01-summary.md](01-summary.md) | What fact is, goals and non-goals for v0 |
| [02-requirements.md](02-requirements.md) | Samuel's requirements R1–R13 and where each is answered |
| [03-temporal.md](03-temporal.md) | Why Temporal, its trade-offs, gates as updates, steering as signals, typed step outcomes |
| [04-architecture.md](04-architecture.md) | The three processes and how they talk |
| [05-domain-model.md](05-domain-model.md) | Project, repository, workflow type, run, step, workspace, change set, runner pool, harness |
| [06-workflow-authoring.md](06-workflow-authoring.md) | Config, step library and workflow code layers; `.fact/`; pinning; the Standard workflow code; review evidence |
| [07-reference-workflow.md](07-reference-workflow.md) | Diagram of the standard workflow |
| [08-planning.md](08-planning.md) | Planning phase: refresh, grill, triage, plan, break down, approve; MCP tools |
| [09-runners-and-harnesses.md](09-runners-and-harnesses.md) | Runner interface, local worktrees, harness contract, model routing, execution zones |
| [10-observability.md](10-observability.md) | Step events, artifact layout, what is measured, caps and monitors |
| [11-ui-and-notifications.md](11-ui-and-notifications.md) | Views, run graph, notifications |
| [12-integrations.md](12-integrations.md) | Task source interface, GitHub Issues, Linear, labels, GitHub App setup |
| [13-storage-and-stack.md](13-storage-and-stack.md) | The tech for each component |
| [14-dogfooding-and-safety.md](14-dogfooding-and-safety.md) | Pinned checkout, guardrails |
| [15-milestones.md](15-milestones.md) | M0–M6, v0.1, v0.2 |
| [16-decisions.md](16-decisions.md) | Decisions and open questions |
| [17-future-ideas.md](17-future-ideas.md) | Ideas not in v0 |
| [18-sources.md](18-sources.md) | Links |
