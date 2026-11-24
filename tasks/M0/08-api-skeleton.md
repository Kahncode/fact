---
id: M0-08
title: "API skeleton with health and projects"
type: feature
complexity: standard
status: planned
depends_on: [M0-04, M0-06, M0-07]
human_actions: []
---

## Goal

`fact.api` is a FastAPI app that starts on the configured port, runs migrations on startup, syncs the project registry into the DB, and serves `/health` and `/api/v1/projects`.

## Read first

- `CLAUDE.md`
- `docs/spec/13-storage-and-stack.md` (Factory API row)
- The template's `api/main.py` and `shared/core/catch_exception_middleware.py`, for structure only (https://github.com/Kahncode/py-fastapi-template)
- `core/src/fact/core/settings.py`, `db.py`, `models.py`, `project_config.py`

## Do

- Add `fastapi[standard]` and `uvicorn[standard]` to `api`.
- `fact/api/app.py`, with `create_app(settings=None)`:
  - lifespan: configure logging, run `migrate.upgrade`, create the engine and session factory on `app.state`, then sync projects;
  - a request-id middleware that binds `request_id` in structlog;
  - an exception handler returning JSON errors.
- Project sync, in `fact/api/projects.py` or a small module: for each `ProjectRef` with a local `path`, load its `.fact/project.yaml`, then upsert the `Project` and its `Repository` rows. Projects without a `path` are upserted from the registry alone, with no repositories.
- Routers under `fact/api/routers/`:
  - `GET /health` returns `{"status": "ok"}`;
  - `GET /api/v1/projects` returns a list of projects with their repositories, as Pydantic response models.
- A DB session dependency.
- `fact/api/__main__.py`, so that `python -m fact.api` runs uvicorn with the settings' host and port.
- Tests use `httpx.AsyncClient` with `ASGITransport`, a temporary `data_dir` and a temporary `fact.yaml` pointing at this repository. Check health, check that projects lists `fact` with `Kahncode/fact`, and check that the sync is idempotent.

## Out of scope

- Runs, steps and events endpoints (M1-03).
- Serving the web UI (M3).
- Auth.

## Acceptance criteria

- [ ] `uv run pytest api` passes.
- [ ] `uv run python -m fact.api` starts, and `GET http://127.0.0.1:8000/api/v1/projects` lists `fact`.
