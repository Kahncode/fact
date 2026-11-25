import http.client
import json
import time
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self

import pytest
from fact.core.claude_usage import (
    ClaudeUsage,
    UsageUnavailableError,
    UsageWindow,
    fetch_claude_usage,
    parse_oauth_usage,
    parse_rate_limit_info,
    read_cli_token,
)

# Trimmed from a real /api/oauth/usage response (24 Nov 2026) and a real stream-json rate_limit_event.
OAUTH = {
    "five_hour": {"utilization": 80.0, "resets_at": "2026-11-25T00:29:59.765318+00:00"},
    "seven_day": {"utilization": 39.0, "resets_at": "2026-11-25T02:59:59.765342+00:00"},
    "seven_day_opus": None,
    "limits": [],
}
STREAM = {
    "status": "allowed",
    "unifiedWindows": {
        "five_hour": {"utilization": 0.01, "resetsAt": 1795566600, "status": "allowed"},
        "seven_day": {"utilization": 0.31, "resetsAt": 1795575600, "status": "allowed"},
    },
}


def test_parse_oauth_usage() -> None:
    usage = parse_oauth_usage(OAUTH)
    assert [w.name for w in usage.windows] == ["five_hour", "seven_day"]
    assert usage.windows[0].utilization == pytest.approx(0.8)
    assert usage.windows[0].resets_at == datetime(2026, 11, 25, 0, 29, 59, 765318, tzinfo=UTC)
    assert usage.summary() == "five_hour 80%, seven_day 39%"


@pytest.mark.parametrize("payload", [{}, {"five_hour": None}, {"five_hour": {"utilization": 1}}])
def test_parse_oauth_usage_rejects_unexpected(payload: dict) -> None:
    with pytest.raises(UsageUnavailableError):
        parse_oauth_usage(payload)


def test_parse_rate_limit_info() -> None:
    usage = parse_rate_limit_info(STREAM)
    assert usage.windows[0] == UsageWindow(
        name="five_hour", utilization=0.01, resets_at=datetime.fromtimestamp(1795566600, tz=UTC)
    )
    assert parse_rate_limit_info({}).windows == ()


def test_blocked_and_resume_at() -> None:
    usage = parse_oauth_usage(OAUTH)
    assert usage.blocked(0.9) == ()
    assert usage.resume_at(0.9) is None
    assert [w.name for w in usage.blocked(0.5)] == ["five_hour"]
    assert usage.resume_at(0.5, margin=timedelta(0)) == usage.windows[0].resets_at
    assert usage.resume_at(0.3, margin=timedelta(minutes=1)) == usage.windows[1].resets_at + timedelta(minutes=1)


def test_rejected_window_is_blocked_below_threshold() -> None:
    window = UsageWindow(name="five_hour", utilization=0.1, resets_at=datetime.now(UTC), rejected=True)
    assert ClaudeUsage(windows=(window,)).blocked(0.9) == (window,)


def _write_credentials(folder: Path, expires_ms: int | None) -> None:
    oauth = {"accessToken": "tok", **({"expiresAt": expires_ms} if expires_ms else {})}
    (folder / ".credentials.json").write_text(json.dumps({"claudeAiOauth": oauth}), encoding="utf-8")


def test_read_cli_token(tmp_path: Path) -> None:
    _write_credentials(tmp_path, int((time.time() + 3600) * 1000))
    assert read_cli_token(tmp_path) == "tok"


def test_read_cli_token_expired_or_missing(tmp_path: Path) -> None:
    with pytest.raises(UsageUnavailableError, match="no Claude Code credentials"):
        read_cli_token(tmp_path)
    _write_credentials(tmp_path, int((time.time() - 60) * 1000))
    with pytest.raises(UsageUnavailableError, match="expired"):
        read_cli_token(tmp_path)


def test_parse_oauth_usage_null_reset_and_naive_datetime() -> None:
    idle = {
        "five_hour": {"utilization": 0.0, "resets_at": None},
        "seven_day": {"utilization": 5, "resets_at": "2026-11-25T00:00:00"},
    }
    usage = parse_oauth_usage(idle)
    assert [w.name for w in usage.windows] == ["seven_day"]
    assert usage.windows[0].resets_at.tzinfo is not None  # naive values are taken as UTC
    with pytest.raises(UsageUnavailableError, match="full but has no reset"):
        parse_oauth_usage({"five_hour": {"utilization": 100, "resets_at": None}})


@pytest.mark.parametrize(
    "info",
    [
        {"unifiedWindows": None},
        {"unifiedWindows": {"five_hour": {"utilization": 0.5}}},  # no resetsAt
        {"unifiedWindows": {"five_hour": {"utilization": 0.5, "resetsAt": None}}},
        {"unifiedWindows": {"five_hour": "bad"}},
    ],
)
def test_parse_rate_limit_info_skips_malformed(info: dict) -> None:
    assert parse_rate_limit_info(info).windows == ()


def test_parse_rate_limit_info_milliseconds_and_rejected() -> None:
    info = {"unifiedWindows": {"five_hour": {"utilization": None, "resetsAt": 1795566600000, "status": "rejected"}}}
    window = parse_rate_limit_info(info).windows[0]
    assert window.resets_at == datetime.fromtimestamp(1795566600, tz=UTC)
    assert window.rejected
    assert window.utilization == 0


def test_merge_keeps_windows_missing_from_newer() -> None:
    old = parse_rate_limit_info(STREAM)
    newer = ClaudeUsage(windows=(old.windows[1].model_copy(update={"utilization": 0.5}),))
    merged = old.merge(newer)
    assert {w.name: w.utilization for w in merged.windows} == {"five_hour": 0.01, "seven_day": 0.5}


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def _stub_urlopen(monkeypatch: pytest.MonkeyPatch, outcome: Any) -> list[Any]:  # noqa: ANN401
    """Replace urlopen (no network in tests); `outcome` is the body to return or an exception to raise."""
    seen: list[Any] = []

    def fake(request: Any, timeout: float) -> _Response:  # noqa: ARG001, ANN401
        seen.append(request)
        if isinstance(outcome, Exception):
            raise outcome
        return _Response(outcome)

    monkeypatch.setattr("urllib.request.urlopen", fake)
    return seen


def test_fetch_claude_usage_success(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _stub_urlopen(monkeypatch, json.dumps(OAUTH).encode())
    assert fetch_claude_usage("secret-token").windows[0].name == "five_hour"
    assert seen[0].get_header("Authorization") == "Bearer secret-token"
    assert seen[0].get_header("Anthropic-beta") == "oauth-2025-04-20"


@pytest.mark.parametrize(
    "outcome",
    [
        urllib.error.HTTPError("https://x", 401, "unauthorized", {}, None),  # type: ignore[arg-type]
        urllib.error.URLError("no route"),
        ConnectionResetError("reset"),
        TimeoutError(),
        http.client.IncompleteRead(b"x"),
        b"not json",
        bytes((0xFF, 0xFE)),
        b"[1, 2]",
    ],
)
def test_fetch_claude_usage_failures_are_wrapped(monkeypatch: pytest.MonkeyPatch, outcome: Any) -> None:  # noqa: ANN401
    _stub_urlopen(monkeypatch, outcome)
    with pytest.raises(UsageUnavailableError) as info:
        fetch_claude_usage("secret-token")
    assert "secret-token" not in str(info.value)


@pytest.mark.parametrize(
    "content",
    [
        bytes((0xFF, 0xFE)),
        b"{}",
        b'{"claudeAiOauth": {}}',
        b'{"claudeAiOauth": {"accessToken": "t", "expiresAt": "soon"}}',
    ],
)
def test_read_cli_token_malformed(tmp_path: Path, content: bytes) -> None:
    (tmp_path / ".credentials.json").write_bytes(content)
    with pytest.raises(UsageUnavailableError):
        read_cli_token(tmp_path)


def test_read_cli_token_uses_config_dir_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_credentials(tmp_path, None)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert read_cli_token() == "tok"
