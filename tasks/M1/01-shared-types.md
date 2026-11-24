---
id: M1-01
title: "Shared run, step and task types"
type: feature
complexity: standard
status: planned
depends_on: [M0-06, M0-09]
human_actions: []
---

## Goal

The types that cross the Temporal boundary (task, workspace, step and run inputs and results) exist once, in `fact.core.types` (re-exported as `fact.sdk.types` for workflow authors), as Pydantic models that the worker, workflows and API all share.

## Read first

- `CLAUDE.md`
- `docs/spec/03-temporal.md` (Internal conventions)
- `docs/spec/05-domain-model.md`
- `docs/spec/09-runners-and-harnesses.md` (Harness contract)
- `core/src/fact/core/enums.py`

## Do

- `core/src/fact/core/types.py`, with Pydantic v2 models, small and JSON-serializable (and `sdk/src/fact/sdk/types.py` re-exporting them):
  - `Task`: `key` (such as `task:M1-01`), `id`, `title`, `body`, `acceptance_criteria` (list of str), `type`, `complexity`, `depends_on`, `path` (optional, for file tasks).
  - `RepoCheckout`: `full_name`, `default_branch`, `branch`. `WorkspaceSpec`: `run_id`, `task_key`, `repos`.
  - `WorkspaceHandle`: `run_id`, `root` (str), `repos` (map from full name to worktree path).
  - `ArtifactRef` (a str key).
  - `Usage`: `tokens_in`, `tokens_out`, `cost_usd`.
  - `StepResult`: `outcome` (reuse `StepOutcome` from `fact.core.enums`), `summary` (short text, at most 2 KB), `outputs` (dict), `log_ref`, `transcript_ref`, `usage`.
  - `PRRef`: `repo`, `number`, `url`, `head_sha`.
  - `RunInput`: `run_id`, `project`, `task`, `repos`, `complexity`.
  - `RunResult`: `outcome`, `prs`, `summary`.
- A `limit_text(s, max_bytes)` helper used wherever text goes into a payload.
- Tests: round-trip every model through Temporal's Pydantic data converter; `limit_text` cuts on a character boundary.

## Out of scope

- Behaviour that uses these types (later M1 tasks).

## Acceptance criteria

- [ ] `uv run pytest core sdk` passes, including the converter round-trip.
- [ ] No model holds raw logs or transcripts, only `ArtifactRef`s.
