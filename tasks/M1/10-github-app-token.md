---
id: M1-10
title: "GitHub App installation tokens"
type: feature
complexity: standard
status: planned
depends_on: [M0-10]
human_actions: 
  - "Download the fact GitHub App private key and keep it outside any repository"
  - "Install the App on Kahncode/fact"
  - "Set FACT_GITHUB_APP_ID and FACT_GITHUB_APP_PRIVATE_KEY_PATH in .env"
---

## Goal

fact authenticates to GitHub as its App: it mints short-lived installation tokens per repository, so PRs, comments and labels are authored by fact[bot] and no personal token is ever used.

## Read first

- `CLAUDE.md`
- `docs/spec/12-integrations.md` (GitHub, GitHub App setup)
- `core/src/fact/core/settings.py`

## Do

- Settings: `github_app_id` and `github_app_private_key_path` (env `FACT_GITHUB_APP_ID`, `FACT_GITHUB_APP_PRIVATE_KEY_PATH`), plus `github_api_url` (default `https://api.github.com`).
- `core/src/fact/core/github/app.py`, with a `GitHubApp` class:
  - App JWT with RS256 (PyJWT with `cryptography`): `iat` 60 s in the past, expiry 9 minutes;
  - `installation_token(full_name)`: look up the repository's installation, then create an access token; cache it until 5 minutes before it expires;
  - `bot_identity()`: the app slug and its bot user's noreply email, for commit authorship.
- `core/src/fact/core/github/client.py`: a thin `httpx.AsyncClient` factory with the auth header, `X-GitHub-Api-Version: 2022-11-28`, and clear errors for 401, 403 and 404.
- CLI: `fact github check <owner/repo>` prints the app slug, the installation and the token expiry, never the token.
- Tests with `httpx.MockTransport` and a throwaway RSA key generated in the test.

## Out of scope

- Pushing and PRs (M1-11).
- Labels (M2-10).
- Webhooks (after hosting).

## Acceptance criteria

- [ ] `uv run pytest core cli` passes.
- [ ] `uv run fact github check Kahncode/fact` succeeds on Samuel's machine.
- [ ] No token value appears in logs (test asserts on captured log output).
