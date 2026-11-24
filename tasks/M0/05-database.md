---
id: M0-05
title: "Async SQLAlchemy and Alembic on SQLite"
type: feature
complexity: standard
status: planned
depends_on: [M0-03]
human_actions: []
---

## Goal

fact has a plain SQLAlchemy 2 async database layer on SQLite (aiosqlite), with Alembic migrations, that agents can extend with ordinary SQLAlchemy code.

## Read first

- `CLAUDE.md`
- `docs/spec/13-storage-and-stack.md` (the Factory DB row)

## Do

- In `core`, add the dependencies `sqlalchemy[asyncio]`, `aiosqlite` and `alembic`.
- `fact/core/db.py`:
  - `Base(DeclarativeBase)` with a naming convention for constraints, so later Postgres migrations are predictable;
  - `create_engine(url)` and `session_factory(engine)` (`async_sessionmaker`, `expire_on_commit=False`);
  - `database_url(settings)` returning `sqlite+aiosqlite:///<db_path>`;
  - SQLite pragmas on connect: `foreign_keys=ON`, `journal_mode=WAL`.

  No repository classes, no service wrapper.
- Alembic in `core/migrations/` with an async `env.py` that reads the URL from settings and `Base.metadata` as its target, and `render_as_batch=True` for SQLite. The `alembic.ini` script location must resolve when run from the repository root.
- `fact/core/migrate.py`, with a function `upgrade(url)` that runs `alembic upgrade head` in code (later used by `fact dev` and by tests).
- Tests: `upgrade()` on a temporary file DB succeeds with zero migrations, and a session can run `SELECT 1`.

## Out of scope

- Tables (M0-06).
- Postgres or Supabase: only keep types Postgres-compatible.

## Acceptance criteria

- [ ] `uv run pytest core` passes.
- [ ] `uv run alembic -c core/alembic.ini current` runs without error against a fresh DB (paths may differ; document the command in `core/README.md`).
