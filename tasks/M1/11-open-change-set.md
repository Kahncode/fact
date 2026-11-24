---
id: M1-11
title: "Push the branch and open the PR"
type: feature
complexity: standard
status: planned
depends_on: [M1-07, M1-10]
human_actions: []
---

## Goal

The control zone pushes a run's branch from its worktree with an installation token and opens, or reuses, a PR with a scripted description. The sandbox never holds GitHub credentials.

## Read first

- `CLAUDE.md`
- `docs/spec/09-runners-and-harnesses.md` (Execution zones, the paragraph under the table)
- `docs/spec/06-workflow-authoring.md` (Review evidence: Scripted PR description)
- `core/src/fact/core/github/app.py`, `client.py`
- `worker/src/fact/worker/activities/workspace.py`

## Do

- An `open_change_set(input) -> list[PRRef]` activity on `control` (input: `run_id`, `task`, `handle`, `title`, `summary`, `check_results`, `run_url`). For v0, one repository.
  - Refuse to push if the branch is the repository's default branch.
  - `git push origin <branch>` from the worktree, with the token passed through `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_0`/`GIT_CONFIG_VALUE_0` as an `http.extraHeader`, so it is in neither argv nor git config. Redact it from any logged output.
  - Find an open PR whose head is the branch: reuse it and update its body. Otherwise create one against the default branch.
  - When the App is configured, `LocalWorktreeRunner` uses `GitHubApp.bot_identity()` as its default commit identity, so agents' commits are authored by fact[bot].
  - The body comes from `worker/src/fact/worker/templates/pr_body.md` (`string.Template`): summary, the task (id, title, link to the task file on the default branch), the acceptance criteria as a checklist, the check results, and the run link.
- Tests: push to a local bare remote (no token in that case); body rendering; create versus reuse against a mocked GitHub API.

## Out of scope

- Several repositories and cross-linked PRs (M4).
- Cost comment (M1-16), CI (M1-19).

## Acceptance criteria

- [ ] `uv run pytest worker` passes.
- [ ] Pushing a default branch is refused (test).
- [ ] The token never appears in argv, in git config or in the step log (test).
