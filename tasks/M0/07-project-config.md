---
id: M0-07
title: "Project config models and fact's own .fact/"
type: feature
complexity: standard
status: planned
depends_on: [M0-03]
human_actions: []
---

## Goal

fact can load a project's `.fact/project.yaml` and each repository's `.fact/repo.yaml`, and fact's own repository has both files, making fact its first project.

## Read first

- `CLAUDE.md`
- `docs/spec/06-workflow-authoring.md` (the "Where projects live" paragraph and the layers table)
- `docs/spec/05-domain-model.md` (Project and Repository rows)
- `core/src/fact/core/settings.py`

## Do

- `fact/core/project_config.py`, with Pydantic models:
  - `ProjectConfig`:
    - `name`;
    - `repositories`: a list of `full_name`, `role`, `default_branch` (default `main`) and `merge_order`;
    - `task_source`: `kind` (`files` for now, also accepting `github`), plus `path` (default `tasks`) or `repo`;
    - `runner_pool` (default `local`);
    - `budget_usd_per_day` (optional);
    - `merge_policy` (`human` only for now);
    - `require_plan_approval` (default true);
    - `workflows`: a map from type label to workflow name, with a `default`.
  - `RepoConfig`: `setup`, `lint`, `test`, `build`, each a list of commands, every command an argv list.
  - Functions `load_project_config(home_path)` and `load_repo_config(repo_path)`, which raise a clear error naming the file and field.
- fact's own `.fact/project.yaml`:
  - one repository, `Kahncode/fact`, role `factory`;
  - `task_source: {kind: files, path: tasks}`;
  - `workflows: {default: standard}`.
- fact's own `.fact/repo.yaml`:
  - `setup: [[uv, sync]]`;
  - `lint: [[uv, run, ruff, check, .], [uv, run, black, --check, .]]`;
  - `test: [[uv, run, pytest]]`.
- Empty `.fact/workflows/` and `.fact/prompts/` folders, each with a short README.
- Tests: load the real files from the repository; reject a project file with a missing name; reject a command given as a string.

## Out of scope

- Loading workflow code (M1-13) and prompts (M1-09).

## Acceptance criteria

- [ ] `uv run pytest core` passes, including a test that loads this repository's own `.fact/` files.
- [ ] Invalid config errors name the file and the field (covered by a test).
