---
id: M2-13
title: "Agent steps wait out Claude usage limits and resume"
type: feature
complexity: complex
status: planned
depends_on: [M2-06]
human_actions: []
---

## Goal

An agent step that hits the Claude Code usage limit is not a failure. The harness spots the limit, the workflow sleeps on a durable Temporal timer until the window resets, and then resumes the same session. fact also records the usage windows it sees in each run, so the scheduler (M2-14) can avoid starting work it can't finish. This is the mid-run half of `scripts/task_loop.py`; M2-14 retires the script.

## Read first

- `CLAUDE.md`
- `docs/spec/10-observability.md` ("Capacity and monitors": the usage window row)
- `core/src/fact/core/claude_usage.py` (`ClaudeUsage`, `parse_rate_limit_info`, `resume_at`)
- `scripts/claude_run.py` (`handle_event`, `LIMIT_TEXT_RE`: how the loop detects a limit today)
- `worker/src/fact/worker/harnesses/claude_code.py`, `worker/src/fact/worker/activities/agent.py`
- `sdk/src/fact/sdk/workflow.py` (`FactWorkflow.agent`) and `sdk/src/fact/sdk/sessions.py`

## Do

- Harness: `ClaudeCodeHarness` parses `rate_limit_event` lines with `parse_rate_limit_info` and merges them into a `ClaudeUsage` on `HarnessResult`. It also sets `rate_limited` when an event has `status: "rejected"`, or when the result is an error whose text matches the usage-limit message (move `LIMIT_TEXT_RE` into `fact.core.claude_usage`).
- Agent activity: a rate-limited run returns a new `StepOutcome.RATE_LIMITED` (in `fact.core.enums`). Its `outputs` carry `resume_at` (ISO, from `ClaudeUsage.resume_at(1.0)`, else now plus 30 min when no window is known) and the step's `session_id`. Record it as its own step attempt, as for any other outcome.
- Usage record: the activity sends the windows it saw to `PUT /api/v1/usage/claude` (new router; account `claude`, one account until runner pools exist). That writes a `usage_windows` table (new migration: account, window name, utilization, resets_at, updated_at). `GET /api/v1/usage` returns the rows. Never store or log a token.
- Workflow: `FactWorkflow.agent` handles `RATE_LIMITED` by sleeping with `workflow.sleep` until `resume_at`, then resuming the session with a context line saying it was paused by a usage limit.
  - These pauses don't count against `MAX_RESUMES`. Cap them at `MAX_LIMIT_PAUSES = 20` per `agent` call; past that the step ends `needs_human`.
  - Waiting has no upper bound: a full 7d window means a wait of days.
  - The run's state while sleeping reads as waiting, with the reason `usage limit until <time>`, visible in `fact status`.
- Tests:
  - harness parsing (fixture stream with a rejected `rate_limit_event`, and one with the limit error result);
  - the activity outcome and the `PUT` call;
  - the API round-trip;
  - workflow in the time-skipping environment: a rate-limited step sleeps until `resume_at`, resumes the same session id, and the pause doesn't use up `MAX_RESUMES`; the pause cap ends in `needs_human`.

## Out of scope

- Starting no new runs while a window is full, `fact scheduler status` and retiring the scripts (M2-14).
- Reading the usage endpoint (M2-14); this task only records what the runs report.
- Other harnesses (M6).

## Acceptance criteria

- [ ] `uv run pytest` passes, including the tests above.
- [ ] A rate-limited agent step resumes the same session after the timer fires, and the run ends `passed` (workflow test with a fake harness).
- [ ] `GET /api/v1/usage` returns the windows from the last run's `rate_limit_event`s (test).
- [ ] `docs/spec/10-observability.md` and `worker/README.md` describe the waiting behaviour.
