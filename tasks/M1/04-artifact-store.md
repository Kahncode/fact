---
id: M1-04
title: "Artifact store for logs and transcripts"
type: feature
complexity: standard
status: planned
depends_on: [M1-03]
human_actions: []
---

## Goal

Logs, transcripts and other step files are stored outside the DB under `runs/<run>/<stage>/<attempt>/`, with only their keys kept in step rows, behind a small interface that can get an S3 backend later.

## Read first

- `CLAUDE.md`
- `docs/spec/10-observability.md` (How data flows)
- `core/src/fact/core/settings.py`

## Do

- `core/src/fact/core/artifacts.py`:
  - an `ArtifactStore` protocol (`put_bytes(key, data)`, `put_file(key, path)`, `open(key)`, `exists(key)`);
  - `LocalArtifactStore(root)` rooted at `settings.artifacts_dir`, rejecting keys that escape the root (`..`, absolute paths, drive letters);
  - `step_key(run_id, stage_id, attempt, name)`.
- API endpoints:
  - `PUT /api/v1/artifacts/{key:path}` (raw body, streamed to disk);
  - `GET /api/v1/artifacts/{key:path}` (a file download, `text/plain` for `.log` and `.jsonl`).
- Tests: put and get round-trip; path-escape attempts rejected, Windows-style ones included; upload and download through the API.

## Out of scope

- S3 or Supabase backends (later).
- Retention: everything is kept forever for now.

## Acceptance criteria

- [ ] `uv run pytest core api` passes.
- [ ] A key like `../x` or `C:\x` is refused with 400 (test).
