---
id: M1-13
title: "Load project workflows and pin versions"
type: feature
complexity: complex
status: planned
depends_on: [M1-12]
human_actions: []
---

## Goal

Each project gets its own workflow worker on task queue `wf-<project>`, which loads `.fact/workflows/*.py` from a checkout of the project's home repository at a fixed SHA. Every run records the factory version and home SHA it is pinned to.

## Read first

- `CLAUDE.md`
- `docs/spec/06-workflow-authoring.md` ("Loading and pinning", "Versions are pinned, with no upgrades", "Changing a workflow")
- `docs/spec/03-temporal.md` (Constant change vs runs in flight)
- `worker/src/fact/worker/__main__.py`
- Temporal Python docs on the workflow sandbox and on worker versioning (check what temporalio 1.36 supports)

## Do

- `worker/src/fact/worker/loader.py`:
  - for each project in the registry, resolve the home repository's default-branch SHA from its mirror (`repos_dir`);
  - create or reuse a detached worktree at `release_dir/projects/<project>/<sha>`;
  - import `.fact/workflows/*.py` with `importlib` under unique module names;
  - collect `FactWorkflow` subclasses decorated with `@workflow.defn`.
- Workflow sandbox: pass `fact.sdk` and pydantic through, so imports work in the sandbox.
- `python -m fact.worker` starts one workflow worker per project on `wf-<project>`, plus the `control` and `sandbox` activity workers. The sandbox worker's concurrent activities are capped at `concurrency.sandboxes`.
- Version id: `<factory git SHA>+<home SHA>`. Recorded on every run (`worker_version`, `home_sha`) through `record_run_started`.
- Pinning: if temporalio 1.36 supports Worker Deployment Versioning with pinned behaviour, use it, with a deployment per project and the version id as build id. If not, record the version only, and have `fact dev` refuse to restart the worker while runs are in flight unless `--force` is given. Write down which you did in `docs/spec/06-workflow-authoring.md`.
- Tests: load workflows from a temporary home repo; a module that fails to import gives a clear error naming the file; the version id format.

## Out of scope

- `fact upgrade` with several worker versions running side by side (later, once runs last long enough to need it).
- Trust limits for client projects (later).

## Acceptance criteria

- [ ] `uv run pytest worker` passes.
- [ ] With `fact dev` running, the Temporal UI shows a worker polling `wf-fact`.
- [ ] The pinning approach chosen is documented in the spec section.
