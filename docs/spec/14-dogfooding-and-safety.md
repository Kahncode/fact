# Dogfooding and safety

The factory's own repo is its first project, so the running factory must never be the code an agent is editing.

- The factory runs from a pinned checkout: `fact use <ref>` makes a detached worktree of the factory repo at `<data_dir>/pinned/factory/<sha>` and marks it current, and `fact dev --pinned` runs the API and worker from it. Agents only ever work in worktrees under `<data_dir>/workspaces/`; the runner refuses a workspace inside `pinned_dir` or the running factory checkout.
- Changes to the factory land through a normal run and its PR, merged by fact when classified `auto_merge` or by a person after review, then `fact use <ref>` and a restart of `fact dev --pinned` start the new worker version. Runs in flight stay pinned to the version they started on and finish on the old one (see `06-workflow-authoring.md` for how pinned versions behave across a restart).
- Changes to the factory follow the same classification as any project (decided 26 Nov 2026): fact bootstraps itself, and a low-risk change to fact merges without a person. The rule that the factory's own changes always need a human merge is retired. A new version still only runs once a person pins it with `fact use` and restarts, or once `fact dev --follow <ref>`, which the person started, upgrades to it when no run is active.
- Stopping the factory, such as before a reboot, goes through `fact shutdown`: the scheduler turns off so no new run starts, the runs in progress finish (waits on a person included), then `fact dev` stops Temporal, the API and the worker and exits by itself. Killing `fact dev` with runs active strands them once the next start pins a newer version. `fact scheduler on` cancels a shutdown; there is no deadline and no forced stop, and an API restart forgets the request.

**Guardrails for every project**

- Budgets per step and per run, a cap on loop iterations, and timeouts on every activity.
- Agents get only the secrets a step needs; model keys and the GitHub token stay with the worker.
- Steps that turn agent permission checks off run in the Docker runner (from v1).
- The token cannot bypass branch protection; there are no pushes to default branches.
