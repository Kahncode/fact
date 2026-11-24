# Dogfooding and safety

The factory's own repo is its first project, so the running factory must never be the code an agent is editing.

- The factory runs from a release checkout (`~/fact/release`, at a tag). Agents only ever work in worktrees under `~/fact/workspaces/`.
- Changes to the factory land through a normal run and a human-merged PR, then `fact upgrade` moves the release checkout and starts the new worker version. Runs in flight finish on the old one.
- Changes to the factory always need a human merge, even if other projects later allow auto-merge.

**Guardrails for every project**

- Budgets per step and per run, a cap on loop iterations, and timeouts on every activity.
- Agents get only the secrets a step needs; model keys and the GitHub token stay with the worker.
- Steps that turn agent permission checks off run in the Docker runner (from v1).
- The token cannot bypass branch protection; there are no pushes to default branches.
