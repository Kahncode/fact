# Planning phase

Planning is everything before execution: refreshing the issue against today's code, clarifying with you (grill-me), triage, the plan with its acceptance criteria, and the breakdown into subtasks. One planning agent does all of it, with full repository context, and the issue always holds the latest plan.

**Full repository context.** The planner works like a Claude Code session with full access to its own workspace holding every repository of the project: it reads the code, runs commands and tests, and can run the app when it needs to. Its changes are never pushed; its only output is the issue. It also reads the context files (README, `docs/`, `AGENTS.md` or `CLAUDE.md`, decision records, `.fact/context/`). Without it, it cannot plan correctly.

**Steps**

1. **Refresh**: read the issue, its comments, and what changed in the repositories since it was written.
2. **Grill**: list the open design questions and sort each into resolved (answered by the issue, the code or the conventions) or open, leaning towards open. Open questions go to the issue as one comment, with `fact:needs-human`, and the run waits for your answers. This step is essential; only trivial issues skip it.
3. **Triage**: a type label on the issue picks the workflow through the label mapping. Without one, the planner proposes the workflow, the complexity level, and the harness and model per step.
4. **Plan**: rewrite the issue description with the plan: context, approach, acceptance criteria, workflow and execution choices. Your original text stays in a collapsed section, and every later planning pass updates the description again.
5. **Break down**: subtasks exist to keep each agent's context small, because quality degrades fast when one session carries too much. Work bigger than one small change becomes sub-issues (GitHub sub-issues or Linear sub-issues), each small enough for one low-complexity run, linked by dependencies. Each subtask is one PR; a dependent subtask waits until the one before it has merged (stacked PRs later). Subtasks are created with `fact:planned` and become `fact:ready` when the parent plan is approved.
6. **Approve**: the workflow switch `require_plan_approval` adds a gate where you approve or edit the plan. It is on for new issues and whenever grilling needed your input; off lets a fully resolved, trivial plan go straight to execution. Approving a parent plan approves its subtasks; each subtask is refreshed when it starts and only asks again if new open questions appear. Unanswered questions wait without a timeout.

   Today (M2-04), with no planner yet, the switch is `require_plan_approval` in `project.yaml` (default true), overridden by the same field in a task's front matter, and passed to the run in `RunInput`. When it is on, `Standard` opens an `approve_plan` gate before any workspace exists, showing the task's title, path and acceptance criteria. Approve goes on; reject ends the run `dropped`. Editing the plan from the gate comes with the planner (M5). fact's own project turns the switch off: setting `status: ready` on a task file is already the approval.

**Acceptance criteria are the contract.** Each criterion is checkable: a test to add or pass, a command and its expected result, or a behaviour a reviewer can verify. The implementation agent works to them, the agent review checks each one, and the run graph shows which passed.

**Dependencies drive scheduling.** An issue runs only when every issue it depends on has merged. The scheduler takes ready issues in dependency order, then by priority, within the caps. On GitHub, dependencies use sub-issues and blocked-by links where available, otherwise a `Depends on #n` list in the plan; on Linear, its blocking relations.

Today (M2-07), the scheduler reads file tasks only. Each tick takes the tasks `ready` on the default branch with every dependency done that have never had a run, orders them by dependency depth, then by id (file tasks have no priority), and starts them within `concurrency.max_runs` and the project's `max_runs`. A task whose run failed, was dropped or had its PR closed is not started again by the scheduler; a person restarts it with `fact run`. The scheduler is created off: `fact scheduler on` turns it on.

**Where you talk to the planner.** In v0, from Claude through the fact MCP server, or on the issue itself; in v1, also from the web UI, and later Slack or Discord.

**v0: plan in Claude**

The factory exposes an MCP server with these tools:

| Tool | Does |
| --- | --- |
| `list_projects` | Projects, their repos and recent runs |
| `read_repo_context` | README, architecture docs and open issues for a project |
| `draft_issues` | Returns proposed issues, with specs, for you to edit; writes nothing |
| `create_issues` | Creates the agreed issues in the project's task source, with the `fact`:ready label if you want them run |
| `start_run` | Starts a run for an issue now |
| `run_status` | Progress, cost and PR links for a run |

Nothing reaches the tracker until you confirm in the conversation, and new issues have no fact:ready label, so nothing runs until you add it.

**v1: planning as a workflow**

- One `PlanningSession` workflow per topic. Each message you send is a Temporal update; the planner agent replies as an activity.
- The transcript lives in the factory DB, not in Temporal history; the session uses continue-as-new every few dozen turns.
- Its outputs are the same: draft issues, then created issues after your yes, then optional runs.
- Channel adapters translate each channel's messages into updates and back: web UI chat first, Slack later, all calling the same API.
