---
id: M1-06
title: "Runner interface and local worktree runner"
type: feature
complexity: standard
status: planned
depends_on: [M1-01]
human_actions: []
---

## Goal

A run gets its own workspace: one git worktree per repository on branch `fact/<task>`, created from a local mirror and resumed if the branch already exists remotely. All of it sits behind the runner interface that later pools implement.

## Read first

- `CLAUDE.md`
- `docs/spec/09-runners-and-harnesses.md` (Runner interface, profiles, Execution zones)
- `docs/spec/05-domain-model.md` (How multi-repo works)
- `core/src/fact/core/types.py`

## Do

- `worker/src/fact/worker/runners/base.py`, a `Runner` protocol with these methods:
  - `prepare(spec) -> WorkspaceHandle`;
  - `exec(handle, argv, cwd, env, timeout)`, an async iterator of output lines with the exit code at the end;
  - `teardown(handle)`;
  - `capabilities()`.
- `worker/src/fact/worker/runners/local_worktree.py`, with `LocalWorktreeRunner(settings)`:
  - mirror at `repos_dir/<owner>/<repo>`: clone if missing, otherwise `git fetch origin`;
  - remote URL from `https://github.com/<full_name>.git`, overridable for tests;
  - for each repo, a worktree at `workspaces_dir/<run_id>/<repo name>` on the task branch, from `origin/<branch>` if it exists, else from `origin/<default_branch>`;
  - set the worktree's commit identity from settings (add `commit_author_name` and `commit_author_email`, defaulting to `fact`);
  - write `WORKSPACE.md` at the workspace root: the repositories, their roles and the branch;
  - `exec` uses `asyncio.create_subprocess_exec` with an argv list, merged stdout and stderr, a timeout that kills the whole process tree (Windows included), and an environment holding only what is passed plus `PATH`, `HOME`, `USERPROFILE` and `SYSTEMROOT`;
  - `teardown` removes the worktrees with `git worktree remove --force` and deletes the run folder.
- Tests use a local bare repository as the remote: fresh branch, resumed branch, an exec that times out, and teardown.

## Out of scope

- Pushing and credentials (M1-11; the sandbox never pushes).
- Docker (M6).

## Acceptance criteria

- [ ] `uv run pytest worker` passes on both CI jobs.
- [ ] A command that never ends is killed at its timeout, and no child process survives (test).
