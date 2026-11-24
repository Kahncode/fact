---
id: M1-09
title: "agent_step activity, model routing and prompts"
type: feature
complexity: complex
status: planned
depends_on: [M1-07, M1-08]
human_actions: []
---

## Goal

Workflows can run an agent step: the harness and model are chosen from the routing table, the project's prompt is rendered with the task, Claude Code runs in the run's workspace, and the step is reported with harness, model, tokens, cost and transcript and ends with a typed outcome.

## Read first

- `CLAUDE.md`
- `docs/spec/09-runners-and-harnesses.md` (Choosing a harness and model, Long agent steps)
- `docs/spec/10-observability.md` (Capacity and monitors: max turns, timeouts)
- `worker/src/fact/worker/harnesses/claude_code.py`, `reporting.py`, `activities/shell.py`

## Do

- Settings, in `fact.yaml`:
  - `routing`: a `default` (`harness: claude-code`, `model: sonnet`) and `rules` keyed `<complexity>.<stage>` with `*` wildcards (for example `complex.implement: {model: opus}`, `"*.review": {model: opus}`);
  - `step_defaults` per kind: `timeout_minutes` and `max_turns`.
- `fact/worker/routing.py`, with `choose(complexity, stage, override=None)`. Order: an explicit override, then `<complexity>.<stage>`, then `<complexity>.*`, then `*.<stage>`, then the default.
- An `agent_step(input) -> StepResult` activity on the `sandbox` queue (input: `run_id`, `stage_id`, `attempt`, `handle`, `task`, `prompt_name`, `context` (str), `complexity`, `override`).
  - Render `.fact/prompts/<prompt_name>.md` from the home repo's worktree with `string.Template`: `$task_id`, `$task_title`, `$task_body`, `$acceptance_criteria`, `$context`, `$workspace`.
  - Run the harness with cwd at the workspace root.
  - Heartbeat every 30 s with the turn count.
  - Upload `transcript.jsonl`.
  - If `.fact-out/<stage_id>.json` exists in the workspace after the run, parse it into `outputs` (and remove it).
  - Outcome:
    - `passed`;
    - `failed_error` (harness error);
    - `stopped_by_monitor` (max turns hit).
- Prompts in `.fact/prompts/`:
  - `implement.md`: implement the task to its acceptance criteria and commit locally with a clear message, never pushing or touching the default branch;
  - `fix_checks.md`: here are the failing checks (`$context`); fix and commit.
- Tests: routing resolution order; prompt rendering; the activity with a fake harness, covering success, error and max turns.

## Out of scope

- Review, update-context and CI prompts (M1-17 to M1-19).
- Budgets that pause runs (M6).

## Acceptance criteria

- [ ] `uv run pytest worker` passes.
- [ ] `choose("complex", "implement")` gives opus and `choose("standard", "implement")` gives sonnet with the shipped `fact.yaml` (test).
- [ ] The step row records harness, model, tokens, cost and `transcript_ref` (test with the fake harness).
