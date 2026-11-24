---
id: M0-10
title: "fact CLI and fact dev"
type: feature
complexity: standard
status: planned
depends_on: [M0-08, M0-09]
human_actions: 
  - "Install the Temporal CLI (temporal on PATH)"
---

## Goal

`fact` is a command-line tool, and `fact dev` starts Temporal, the API and the worker together with one command on any OS.

## Read first

- `CLAUDE.md`
- `docs/spec/13-storage-and-stack.md` (the CLI row and the Tooling paragraph)
- `core/src/fact/core/settings.py`

## Do

- Add `typer` and `httpx` to `cli`, and a `fact = "fact.cli.main:app"` script entry point. The Typer app lives in `cli/src/fact/cli/main.py`, with `fact dev` in `cli/src/fact/cli/dev.py`.
- `fact dev`:
  - creates `data_dir`, then starts three child processes: `temporal server start-dev --db-filename <temporal_db_path> --ui-port 8233`, then `python -m fact.api`, then `python -m fact.worker` (using `sys.executable`);
  - prefixes each process's output lines with its name;
  - stops all of them when one exits or on Ctrl-C. On Windows, start children in a new process group and send `CTRL_BREAK_EVENT`; elsewhere send SIGINT, then terminate after a timeout;
  - waits for Temporal's port before starting the worker.

  Fails with a clear message if `temporal` isn't on PATH.
- `fact projects list`: calls `GET /api/v1/projects` and prints a table (name, home, repositories).
- `fact --version`.
- Tests: the command lines `fact dev` builds (no processes started), and `projects list` against a mocked API (`httpx.MockTransport`).

## Out of scope

- `fact run` (M1-14), `fact release` (M1-15), gate commands (M2-02).

## Acceptance criteria

- [ ] `uv run pytest cli` passes.
- [ ] On Samuel's Windows PC, `uv run fact dev` brings up all three, the Temporal UI loads on port 8233, `uv run fact projects list` shows `fact`, and Ctrl-C leaves no process running.
