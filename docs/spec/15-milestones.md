# v0 scope and milestones

v0 is done when the factory has shipped one of its own GitHub issues end to end, and your only touches were answering the planner, approving the plan and merging the PR.

1. **M0 Skeleton**: Temporal dev server, worker, FastAPI, SQLite schema (projects, runs, steps, events), the fact project config.
2. **M1 First run**: `fact run <TASK-KEY>` → prepare workspace → implement → lint and test loop → agent review → learn → PR → CI fix loop; you merge. Tasks come from markdown files in `tasks/` until GitHub Issues is live. Time, cost and logs recorded per step. From late M1, fact builds its own tasks.
3. **M2 Issues and gates**: gates and steering answered through the API or CLI (including plan approval), a scheduler that starts ready tasks in dependency order and waits out Claude Code usage windows (retiring `scripts/task_loop.py`), then the GitHub Issues task source with the label trigger by polling and status and comment write-back; the remaining task files move to GitHub issues.
4. **M3 Classify and auto-merge** (moved ahead of the UI on 26 Nov 2026): the classify stage and its signals, auto-merge on by default (`06-workflow-authoring.md`).
5. **M4 UI**: the web app covers everything a person does (`11-ui-and-notifications.md`), and the CLI keeps only `fact dev` and `fact use`.
6. **M5 Loops and multi-repo**: the @fact comment trigger, change sets across several repos.
7. **M6 Planner**: the planning phase as a workflow (refresh, grill, triage, plan, break down into subtasks) and the MCP server for planning from Claude. Until then, Claude Code in the repository plans, with the plan-tasks skill.
8. **M7 Hardening:** secrets management (to design), Docker runner, a second harness (Codex), budgets that pause runs, the Costs view. Linear, the second task source, is in the spec but not yet scheduled.

**v0.1**: a second runner pool.

**v0.2**: scoring and benchmarks: LLM-judge scorers on every run, and a set of past issues replayed against a new version of fact (prompts, workflows, models) to show whether it is better before it is adopted.
