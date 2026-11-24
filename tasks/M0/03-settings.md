---
id: M0-03
title: "Settings and fact.yaml loading"
type: feature
complexity: standard
status: planned
depends_on: [M0-01]
human_actions: []
---

## Goal

`fact.core.settings` loads fact's global settings from `fact.yaml`, an optional per-environment overlay, `FACT_*` environment variables and `.env`, so every process reads its configuration the same way.

## Read first

- `CLAUDE.md`
- `docs/spec/13-storage-and-stack.md` (the Config row)
- The template's `shared/core/config.py` and `shared/core/app_environment.py`, for the idea only (https://github.com/Kahncode/py-fastapi-template)

## Do

- In `core`: `fact/core/settings.py` with Pydantic v2 / pydantic-settings models:
  - `Settings`: `env` (`local`, `test`, `dev`, `prod`; from `FACT_ENV`, default `local`), `data_dir` (default `~/fact`, expanded with pathlib), `temporal` (`address` default `localhost:7233`, `namespace` default `default`), `api` (`host` `127.0.0.1`, `port` `8000`), `concurrency` (`sandboxes` 2, `planners` 1), `projects` (a list of `ProjectRef`: `name`, `home` such as `Kahncode/fact`, optional local `path`).
  - Derived paths as properties: `db_path` (`data_dir/fact.db`), `temporal_db_path` (`data_dir/temporal.db`), `artifacts_dir`, `workspaces_dir`, `repos_dir`, `release_dir`.
- Load order, lowest to highest: model defaults, `fact.yaml`, then `fact.<env>.yaml` if present (both next to the repository root, or at the path in `FACT_CONFIG`), then environment variables with the `FACT_` prefix and `__` as the nesting delimiter, then `.env`.
- `get_settings()` returns a cached instance, and tests can reset it.
- A starter `fact.yaml` at the repository root with the defaults written out and one project: `name: fact`, `home: Kahncode/fact`, `path: .`.
- `.env.example` listing the variables later tasks use: `FACT_GITHUB_APP_ID`, `FACT_GITHUB_APP_PRIVATE_KEY_PATH`.
- Tests: defaults, YAML loading, the env overlay, env-var override (`FACT_TEMPORAL__ADDRESS`), and `~` expansion, all using `tmp_path`.

## Out of scope

- Project config (`.fact/project.yaml`) is M0-07.
- Model routing settings come in M1-09.

## Acceptance criteria

- [ ] `uv run pytest core` passes with the tests above.
- [ ] `uv run python -c "from fact.core.settings import get_settings; print(get_settings().db_path)"` prints a path under your home folder.
- [ ] `FACT_TEMPORAL__ADDRESS=x:1` changes `get_settings().temporal.address` (covered by a test).
