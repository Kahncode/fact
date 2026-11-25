# Workflows

This project's workflow code (`*.py`), loaded by its workflow worker from a checkout of this repository. Each workflow subclasses `FactWorkflow` from `fact.sdk.workflow`, declares its stages with `@stages([...])` and imports shared types from `fact.sdk.types`.

- `standard.py`: `Standard` (Temporal type `standard`). Prepare the workspace, run `implement`, loop lint and test with `fix_checks` sessions (at most 3) until green, mark the task done on the branch, open the PR. `agent` resumes a session once when the worker stopped during it or the agent reports a temporary, retryable block (see `fact.sdk.sessions`). Outcomes: `pr_opened`; `needs_human` (the implement agent reported itself blocked, or checks never went green; no PR); `failed` (implement errored or hit its turn limit).

Workflow code is replayed by Temporal: no I/O, clock or settings reads; timeouts come from `RunInput.limits`. A change to a workflow must keep the recorded histories replaying: `uv run pytest worker/tests/test_standard_workflow.py`, and re-record after an intended change with `FACT_RECORD_HISTORIES=1`.

`python -m fact.worker` loads these files from a detached checkout of the default branch's SHA (`pinned_dir/projects/<project>/<sha>`) and runs them on task queue `wf-<project>`, pinned to that version (see `worker/README.md`). Files starting with `_` are skipped; a file that fails to import stops the whole project from loading. Only classes that subclass `FactWorkflow` and are decorated with `@workflow.defn` themselves are registered. A change merged to the default branch takes effect at the next worker restart; runs already pinned to the old version then wait until they are dropped and restarted.
