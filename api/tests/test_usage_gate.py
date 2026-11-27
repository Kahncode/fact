"""`UsageGate` in the API: when it calls the usage endpoint, how a full window holds, and when it is off."""

from datetime import UTC, datetime, timedelta

import pytest
from fact.api.usage_gate import MIN_WAIT, RESET_MARGIN, UsageGate
from fact.core.claude_usage import ClaudeUsage, UsageUnavailableError
from fact.core.settings import ClaudeSettings
from fact.core.usage_gate import GateState
from scheduler_env import FakeUsage, five_hour

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def _gate(usage: FakeUsage, claude: ClaudeSettings | None = None, environ: dict[str, str] | None = None) -> UsageGate:
    return UsageGate(claude or ClaudeSettings(), fetch=usage, environ=environ or {})


async def test_full_window_waits_for_its_reset_without_calling() -> None:
    usage = FakeUsage(five_hour(0.95, NOW + HOUR))
    gate = _gate(usage)
    first = await gate.check(NOW)
    assert first.state == GateState.FULL
    assert "five_hour 95% (reserve 10%)" in first.reason
    assert (await gate.check(NOW + HOUR / 2)).state == GateState.FULL
    assert usage.calls == 1  # resets_at is still ahead: no call
    usage.answer = five_hour(0.02, NOW + 6 * HOUR)
    assert (await gate.check(NOW + HOUR + RESET_MARGIN)).state == GateState.OK
    assert usage.calls == 2  # one call confirms the reset  # noqa: PLR2004


async def test_window_still_full_past_its_reset_backs_off() -> None:
    usage = FakeUsage(five_hour(0.99, NOW - RESET_MARGIN - timedelta(minutes=1)))  # reset passed, still full
    gate = _gate(usage)
    at = NOW
    for wait, calls in [
        (timedelta(0), 1),
        (MIN_WAIT - timedelta(seconds=10), 1),  # backing off for MIN_WAIT
        (timedelta(seconds=20), 2),
        (MIN_WAIT + timedelta(seconds=30), 2),  # then for twice as long
        (MIN_WAIT, 3),
    ]:
        at += wait
        assert (await gate.check(at)).state == GateState.FULL
        assert usage.calls == calls


async def test_unreadable_usage_is_unavailable_not_full() -> None:
    def refuse() -> ClaudeUsage:
        msg = "no Claude Code credentials in ~/.claude"
        raise UsageUnavailableError(msg)

    check = await UsageGate(ClaudeSettings(), fetch=refuse, environ={}).check(NOW)
    assert check.state == GateState.UNAVAILABLE
    assert "credentials" in check.error


@pytest.mark.parametrize(
    ("claude", "environ"),
    [
        (ClaudeSettings(usage_gate=False), {}),
        (ClaudeSettings(), {"ANTHROPIC_API_KEY": "sk-test"}),  # pragma: allowlist secret
    ],
)
async def test_gate_off(claude: ClaudeSettings, environ: dict[str, str]) -> None:
    usage = FakeUsage(five_hour(1.0, NOW + HOUR))
    assert (await _gate(usage, claude, environ).check(NOW)).state == GateState.OFF
    assert usage.calls == 0
