---
id: M2-09
title: "GitHub Issues task source: reading"
type: feature
complexity: standard
status: planned
depends_on: [M2-08]
human_actions: []
---

## Goal

fact can read work from GitHub Issues: open issues labelled `fact:ready` become tasks, their dependencies come from GitHub's blocked-by links (or a `Depends on #n` list), and they can be started like file tasks.

## Read first

- `CLAUDE.md`
- `docs/spec/12-integrations.md` (GitHub Issues, labels)
- `docs/spec/08-planning.md` ("Dependencies drive scheduling")
- `core/src/fact/core/tasks/base.py`, `core/src/fact/core/github/client.py`

## Do

- `core/src/fact/core/tasks/github_issues.py`, with `GitHubIssuesSource(repo, app)`:
  - `list_ready()`: open issues with `fact:ready`, excluding PRs.
  - `get(key)`: a `Task`. Type comes from the `type:*` label and complexity from the `complexity:*` label (default standard). Acceptance criteria come from the checklist under an "Acceptance criteria" heading.
  - `deps_done(task)`:
    - blocking issues come from the REST issue dependencies API (`GET /repos/{owner}/{repo}/issues/{n}/dependencies/blocked_by`; check it against GitHub's docs);
    - also from `Depends on #n` lines;
    - done means closed as completed.
- Branch `fact/gh-<n>`, keys `gh:owner/repo#n`.
- The scheduler and `fact run` accept `gh:` keys for projects whose `task_source.kind` is `github`.
- Tests with `httpx.MockTransport`: listing, parsing, both dependency forms.

## Out of scope

- Writing to issues (M2-11).
- Moving fact's own tasks over (M2-12).

## Acceptance criteria

- [ ] `uv run pytest core` passes.
- [ ] `fact run gh:Kahncode/fact#<n>` on a throwaway issue starts a run (checked by hand).
