# Planning phase

Planning is everything before execution: refreshing the issue against today's code, clarifying with you (grill-me), triage, the plan with its acceptance criteria, and the breakdown into subtasks. One planning agent does all of it, with full repository context, and the issue always holds the latest plan.

**Full repository context.** The planner works like a Claude Code session with full access to its own workspace holding every repository of the project: it reads the code, runs commands and tests, and can run the app when it needs to. Its changes are never pushed; its only output is the issue. It also reads the context files (README, `docs/`, `AGENTS.md` or `CLAUDE.md`, decision records, `.fact/context/`). Without it, it cannot plan correctly.

**Steps**

1. **Refresh**: read the issue, its comments, and what changed in the repositories since it was written.
2. **Grill**: list the open design questions and sort each into resolved (answered by the issue, the code or the conventions) or open, leaning towards open. Open questions go to the issue as one comment, with `fact:needs-human`, and the run waits for your answers. This step is essential; only trivial issues skip it.
3. **Triage**: a type label on the issue picks the workflow through the label mapping. Without one, the planner proposes the workflow, the complexity level, and the harness and model per step. The planner also asks you for the issue's priority, suggesting one, and sets the `priority:*` label you confirm. Sub-issues need no label of their own: they are their parent's dependencies, so they inherit its effective priority; the planner labels one only when it should rank above its parent.
4. **Plan**: rewrite the issue description with the plan: context, approach, acceptance criteria, workflow and execution choices. Your original text stays in a collapsed section, and every later planning pass updates the description again.
5. **Break down**: subtasks exist to keep each agent's context small, because quality degrades fast when one session carries too much. Work bigger than one small change becomes sub-issues (GitHub sub-issues or Linear sub-issues), each small enough for one low-complexity run, linked by dependencies. Each subtask is one PR; a dependent subtask waits until the one before it has merged (stacked PRs later). Sub-issues are created `fact:ready` under a parent created `fact:planned`: the parent's label is the gate (see Parent issues below).
6. **Approve**: the workflow switch `require_plan_approval` adds a gate where you approve or edit the plan. It is on for new issues and whenever grilling needed your input; off lets a fully resolved, trivial plan go straight to execution. Approving a parent plan means setting the parent `fact:ready`, which releases its ready sub-issues; each subtask is refreshed when it starts and only asks again if new open questions appear. Unanswered questions wait without a timeout.

   Today (M2-04), with no planner yet, the switch is `require_plan_approval` in `project.yaml` (default true), overridden by the same field in a task's front matter, and passed to the run in `RunInput`. When it is on, `Standard` opens an `approve_plan` gate before any workspace exists, showing the task's title, path and acceptance criteria. Approve goes on; reject ends the run `stopped`. Editing the plan from the gate comes with the planner (M6). fact's own project turns the switch off: setting `status: ready` on a task file is already the approval.

**Acceptance criteria are the contract.** Each criterion is checkable: a test to add or pass, a command and its expected result, or a behaviour a reviewer can verify. The implementation agent works to them, the agent review checks each one, and the run graph shows which passed.

**Dependencies drive scheduling.** An issue runs only when every issue it depends on has merged. The scheduler orders each project's ready issues on its own: by effective priority (`05-domain-model.md`), then dependency depth, then id, within the caps. Projects do not compete: each will get its own runner pool. Until then the global `concurrency.max_runs` is shared, and when it is the binding cap the projects take turns (round-robin), each offering its next task; this is temporary. Effective priority follows dependencies only within the project. On GitHub an issue with several `priority:*` labels takes the highest. On GitHub, dependencies use sub-issues and blocked-by links where available, otherwise a `Depends on #n` list in the plan; on Linear, its blocking relations.

Today (M2-07, GitHub Issues since M2-09), each tick takes the tasks that are ready (file tasks `ready` on the default branch, or open issues labelled `fact:ready`) with every dependency done and no run in progress, orders them by dependency depth, then by id (file ids, then issue numbers; no priority yet, and one list across projects), and starts them within `concurrency.max_runs` and the project's `max_runs`, unless a Claude usage window is inside its reserve. A task whose runs failed is retried until `concurrency.max_failed_runs` (default 3) failed in a row (M2-14); a task whose last run needed a person, was stopped, or opened, merged or closed its PR is not started again by the scheduler; a person restarts it with `fact run`. The scheduler is created off: `fact scheduler on` turns it on, and `fact scheduler status` previews what it would start next.

**Parent issues and sub-issues** (decided 26 Nov 2026, not built yet: today the GitHub source ignores sub-issue links and runs any `fact:ready` issue whose blocked-by dependencies are done).

- fact runs only issues labelled `fact:ready`, never anything else.
- **The parent's label is the gate, transitively.** A sub-issue runs only when it and every ancestor are `fact:ready`. A ready sub-issue under a `fact:planned` parent waits, so approving the plan is one label change on the parent.
- **Sub-issues are dependencies of their parent.** Only open sub-issues count: a closed one, whatever its close reason, is out of the parent's way. Once none is open, the ready parent runs as a normal task, for example an integration or verification pass.
- **A run with nothing to do succeeds.** It ends `no_changes`: no PR, the issue is closed as completed with a comment saying why, and the `run_finished` notification has severity `warning` so a person can inspect it.
- **Epics** (`type:epic`) have no work of their own. Once no sub-issue is open and the epic is `fact:ready`, fact closes it as completed without a run.
- **Blocked-by links stay strict.** A blocker closed as completed unblocks its dependent. A blocker closed for any other reason (not planned, duplicate) does not: fact adds `fact:needs-human` to the dependent and comments which blocker it was, and never starts it on its own.
- **`fact:needs-human` holds an issue until a person answers.** The scheduler never starts an issue carrying it and `fact run` refuses one. Only a person's answer clears it: answering the gate with `fact gate`, or removing the label by hand. Run ends never remove it, so a stopped or closed run on an issue that needed a person still shows it if the task is started again.

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

**Until M6: plan in Claude Code with skills.** Grilling (`/grill-with-docs`) updates `docs/spec/` as decisions are made; `/to-spec` writes the parent issue and `/to-tickets` its sub-issues, after you confirm the breakdown in the conversation. Leaf issues are created `fact:ready`, parents `fact:planned`. Rules for agents are in `docs/agents/issue-tracker.md`.

**v1: planning as a workflow**

- One `PlanningSession` workflow per topic. Each message you send is a Temporal update; the planner agent replies as an activity.
- The transcript lives in the factory DB, not in Temporal history; the session uses continue-as-new every few dozen turns.
- Its outputs are the same: draft issues, then created issues after your yes, then optional runs.
- Channel adapters translate each channel's messages into updates and back: web UI chat first, Slack later, all calling the same API.
