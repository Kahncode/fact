# Domain model

A project is a set of repos, and a run works on all of them at once in one workspace, producing one change set of PRs.

**Terms.** A *project* is a factory concept: one product, with its workflows, triage rules and budgets. A *repository* is a GitHub repo. A project has one or more repositories, and a run can touch several of them, so every link the factory shows (branch, PR, CI run) belongs to one repository. Linear projects and GitHub Projects are only mapped to factory projects, never the same thing. A *task* is an issue (or task file) fact may run. A *parent issue* has sub-issues, which are its dependencies; an *epic* (`type:epic`) is a parent with no work of its own. Sub-issues and blocked-by links are different: see `08-planning.md`. A *notification* is a message the factory sends a person because something needs them (a gate, PRs that need review, a failed run) or is worth checking (a warning); routine progress never notifies. A notification is *read* once the person has seen it and *resolved* once what it asked for no longer applies (the gate was answered, the PRs merged or closed, the failed run was stopped or restarted); the factory resolves it, the person never has to. The *inbox* is the list of unresolved notifications. A notification is addressed to a project (everyone on it) for now; later to a person, such as the one who started the run. A run's *started by* is a person or the scheduler. A run is *stopped* (decided 26 Nov 2026): it ends where it is, keeps its branch and PR and leaves a handoff note; a stopped run is never restarted, but a new run of the same task can start later and resumes from the note. A task is *dropped* when it is closed without being done. A task has at most one active run at a time. Tasks are dropped only in the tracker, never by fact; a run whose task was closed while it ran finishes, and its change set then needs a human review (classify checks that the task is still open).

| Entity | What it is | Key fields |
| --- | --- | --- |
| Project | A product the factory works on | name, repositories, mapped task source (GitHub repository or Linear team), workflow catalog, triage rules, preview deploy command, runner pool, budgets, merge policy, policies |
| Repository | One GitHub repo inside a project | URL, role (backend, mobile, web, infra), default branch, setup, test and build commands, merge order |
| Workflow type | A Python workflow class, one or more per project | name, stage manifest, worker version (factory git SHA) |
| Run | One Temporal workflow execution for one work item | workflow id `run-<ISSUE-KEY>-<n>`, project, issue, status, totals for time, cost and human touches |
| Step | One activity or gate inside a run, per attempt | kind (agent, shell, github, gate, notify), attempt, start, end, duration, wait time, tokens, cost, log and transcript refs, outputs |
| Workspace | The per-run folder | one git worktree per repo, all on branch `fact/<ISSUE-KEY>` |
| Change set | The PRs a run produces | one PR per touched repo, cross-linked, with a merge order |
| Runner pool | Where a run's sandbox lives: your PC, containers, GitHub runners, Daytona | kind, concurrency cap, secrets |
| Harness | Which coding agent runs an agent step | Claude Code, Codex, OpenCode, Pi; model; allowed tools; chosen per task or per step (see Choosing a harness and model) |

**How multi-repo works**

- The project config lists its repos; a run checks out a worktree of each repo it needs, side by side, so one agent can see the backend and the mobile app together.
- The agent sees one workspace folder with a short `WORKSPACE.md` describing the repos and how they relate.
- At PR time the factory opens one PR per repo that changed, links them to each other in their descriptions, and records a merge order (for example backend before mobile) that the UI shows.
