---
id: M0-01
title: "uv workspace and dev tooling"
type: chore
complexity: standard
status: planned
depends_on: []
human_actions: []
---

## Goal

The repository becomes a uv workspace on Python 3.15 with five empty packages (`core`, `api`, `worker`, `sdk`, `cli`) under the `fact.*` namespace, and the linting, formatting and test tooling from Samuel's FastAPI template.

## Read first

- `CLAUDE.md`
- `docs/spec/13-storage-and-stack.md` (the Dev tooling and Tests rows)
- From the template (https://github.com/Kahncode/py-fastapi-template): `pyproject.toml`, `.pre-commit-config.yaml`, `.gitignore`, `.gitattributes`, `.secrets.baseline`

## Do

- Root `pyproject.toml`: a uv workspace (`[tool.uv.workspace] members = ["core", "api", "worker", "sdk", "cli"]`), `requires-python = ">=3.15"`, a dev dependency group (pytest, pytest-asyncio, ruff, black, pre-commit, detect-secrets), and the template's ruff, black and pytest settings with targets moved to Python 3.15 (or the newest target each tool supports). Use `asyncio_mode = "auto"`, and point pytest's `testpaths` at every package's `tests/`.
- `.python-version` with `3.15`.
- One member per folder, each with a `pyproject.toml` (hatchling) and the layout `<pkg>/src/fact/<pkg>/__init__.py` plus `<pkg>/tests/test_import.py`. `fact` is an implicit namespace package: no `src/fact/__init__.py`. Members depend on each other as workspace sources: `api`, `worker` and `cli` depend on `core`; `worker` depends on `sdk`; `sdk` depends on `core`.
- `.gitignore` and `.gitattributes` from the template, plus `.env`, `*.db` and `node_modules/`. `.pre-commit-config.yaml` from the template (ruff, black, detect-secrets), and a fresh `.secrets.baseline`.
- A short `README.md` at the root: what fact is (one paragraph), `uv sync`, and how to run the checks.
- `uv.lock` committed.

## Out of scope

- No application code: settings, logging, DB, API and worker are later M0 tasks.
- CI (M0-02).
- Don't copy the template's `shared/`, `api/` or `scripts/` code.

## Acceptance criteria

- [ ] `uv sync` succeeds from a fresh clone on Python 3.15.
- [ ] `uv run python -c "import fact.core, fact.api, fact.worker, fact.sdk, fact.cli"` succeeds.
- [ ] `uv run ruff check .`, `uv run black --check .` and `uv run pytest` pass, and pytest collects the five import tests.
- [ ] `uv run pre-commit run --all-files` passes.
- [ ] No `.sh`, `.bat` or Makefile in the repository.
