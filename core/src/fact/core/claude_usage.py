"""
Claude Code subscription usage: the 5-hour and weekly windows, and when they reset.

Two sources feed the same `ClaudeUsage` model:

- `fetch_claude_usage(token)` calls the account's usage endpoint with an OAuth token. fact passes the token from a
  secret; nothing here reads it from disk. The endpoint is undocumented and may change.
- `parse_rate_limit_info(info)` reads the `rate_limit_event` that `claude -p --output-format stream-json` emits, which
  needs no token and costs nothing extra when a run is already in progress.

`ClaudeUsage.resume_at()` says when everything is below a threshold again, so a caller can sleep (or a workflow
can start a durable timer) until exactly then.

`read_cli_token()` is a local-development convenience that reads the token Claude Code itself stored.
"""

import http.client
import json
import os
import re
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
OAUTH_WINDOWS = ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet")
MILLIS_THRESHOLD = 1e11  # an epoch above this (year 5138 in seconds) is in milliseconds
# an error result whose text matches this hit a usage or rate limit (also an API 429): wait, then resume
LIMIT_TEXT_RE = re.compile(r"usage limit|rate limit|limit reached|hit your limit", re.IGNORECASE)
LIMIT_FALLBACK = timedelta(minutes=30)  # how long to wait for a limit whose reset is unknown or already past


class UsageUnavailableError(Exception):
    """Usage could not be read (no or expired token, network error, unexpected response)."""


class UsageWindow(BaseModel):
    """One usage window. `utilization` is 0..1; `rejected` means the account is already being refused."""

    model_config = ConfigDict(frozen=True)

    name: str
    utilization: float
    resets_at: datetime
    rejected: bool = False

    @field_validator("resets_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        """Take a reset time without an offset as UTC, so windows always compare."""
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    def is_blocked(self, threshold: float) -> bool:
        return self.rejected or self.utilization >= threshold


class ClaudeUsage(BaseModel):
    model_config = ConfigDict(frozen=True)

    windows: tuple[UsageWindow, ...] = ()

    def blocked(self, threshold: float) -> tuple[UsageWindow, ...]:
        """Windows at or over the threshold (0..1), or already rejecting."""
        return tuple(w for w in self.windows if w.is_blocked(threshold))

    def resume_at(self, threshold: float, margin: timedelta = timedelta(seconds=90)) -> datetime | None:
        """When every blocked window has reset (plus a margin); None when nothing is blocked."""
        blocked = self.blocked(threshold)
        return max(w.resets_at for w in blocked) + margin if blocked else None

    def merge(self, newer: ClaudeUsage) -> ClaudeUsage:
        """Windows by name, the newer one winning; windows only in `self` are kept."""
        by_name = {w.name: w for w in (*self.windows, *newer.windows)}
        return ClaudeUsage(windows=tuple(by_name.values()))

    def summary(self) -> str:
        return ", ".join(f"{w.name} {w.utilization:.0%}" for w in self.windows) or "unknown"


def _oauth_window(name: str, entry: dict[str, Any]) -> UsageWindow | None:
    """One endpoint window. None for a window without a reset time that is not full (nothing to wait for)."""
    percent = float(entry["utilization"])  # 0..100 here, unlike the stream's 0..1
    if entry.get("resets_at") is None:
        if percent >= 100:  # noqa: PLR2004
            msg = f"{name} is full but has no reset time"
            raise ValueError(msg)
        return None
    return UsageWindow(name=name, utilization=percent / 100, resets_at=datetime.fromisoformat(entry["resets_at"]))


def parse_oauth_usage(payload: dict[str, Any]) -> ClaudeUsage:
    """Parse the usage endpoint response (utilization is a percent there)."""
    windows = []
    for name in OAUTH_WINDOWS:
        entry = payload.get(name)
        if not isinstance(entry, dict):
            continue
        try:
            window = _oauth_window(name, entry)
        except (KeyError, TypeError, ValueError) as exc:
            msg = f"unexpected usage entry for {name}: {exc!r}"
            raise UsageUnavailableError(msg) from exc
        if window:
            windows.append(window)
    if not windows:
        msg = "usage response has no known windows"
        raise UsageUnavailableError(msg)
    return ClaudeUsage(windows=tuple(windows))


def _stream_window(name: str, w: dict[str, Any]) -> UsageWindow | None:
    """One stream window; None when it is malformed or has no reset time."""
    try:
        resets = float(w["resetsAt"])
        resets = resets / 1000 if resets > MILLIS_THRESHOLD else resets
        return UsageWindow(
            name=name,
            utilization=float(w.get("utilization") or 0.0),
            resets_at=datetime.fromtimestamp(resets, tz=UTC),
            rejected=w.get("status") == "rejected",
        )
    except KeyError, TypeError, ValueError, OverflowError, OSError:
        return None


def parse_rate_limit_info(info: dict[str, Any]) -> ClaudeUsage:
    """Parse `rate_limit_event.rate_limit_info` from Claude Code's stream-json output (empty when it has no windows)."""
    raw = info.get("unifiedWindows")
    entries = raw.items() if isinstance(raw, dict) else ()
    windows = (_stream_window(name, w) for name, w in entries if isinstance(w, dict))
    return ClaudeUsage(windows=tuple(w for w in windows if w))


def fetch_claude_usage(token: str, *, timeout: float = 15) -> ClaudeUsage:
    """Read the account's usage windows with an OAuth token (blocking; use `asyncio.to_thread` in async code)."""
    request = urllib.request.Request(
        USAGE_URL, headers={"Authorization": f"Bearer {token}", "anthropic-beta": OAUTH_BETA, "User-Agent": "fact"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            payload = json.loads(response.read())
    except (OSError, http.client.HTTPException, ValueError) as exc:  # OSError covers URLError and timeouts
        msg = f"could not read Claude usage: {exc!r}"
        raise UsageUnavailableError(msg) from exc
    if not isinstance(payload, dict):
        msg = "usage response is not an object"
        raise UsageUnavailableError(msg)
    return parse_oauth_usage(payload)


def read_cli_token(config_dir: Path | None = None) -> str:
    """
    Local development only: the access token Claude Code stored in `<config dir>/.credentials.json`.

    It is short-lived and only the `claude` CLI refreshes it. Not available where the CLI keeps credentials in a
    keychain (macOS); fact itself uses a secret instead.
    """
    folder = config_dir or Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    try:
        oauth = json.loads((folder / ".credentials.json").read_text(encoding="utf-8"))["claudeAiOauth"]
        token, expires_ms = str(oauth["accessToken"]), float(oauth.get("expiresAt") or 0)
    except (OSError, KeyError, TypeError, ValueError) as exc:  # ValueError covers bad JSON, UTF-8 and numbers
        msg = f"no Claude Code credentials in {folder}"
        raise UsageUnavailableError(msg) from exc
    if expires_ms and expires_ms / 1000 < time.time():
        msg = "the stored Claude Code token has expired; run `claude` once to refresh it"
        raise UsageUnavailableError(msg)
    return token
