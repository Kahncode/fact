"""
The scheduler's Claude usage gate: what the `check_claude_usage` activity answers, and the per-window thresholds.

A window is full when its utilization is at or above `1 - reserve` (`claude.reserve.five_hour` for `five_hour`,
`claude.reserve.weekly` for the three weekly windows), or when it is rejecting. The worker keeps the last answer
(`fact.worker.usage_gate`) and reports it to the API, where `fact scheduler status` reads it.
"""

from datetime import UTC, datetime
from enum import StrEnum

from fact.core.claude_usage import ClaudeUsage
from fact.core.settings import ClaudeSettings
from pydantic import BaseModel, ConfigDict, Field

WEEKLY_WINDOWS = ("seven_day", "seven_day_opus", "seven_day_sonnet")


class GateState(StrEnum):
    OK = "ok"  # no window is full: runs may start
    FULL = "full"  # a window is inside its reserve: the scheduler starts nothing
    UNAVAILABLE = "unavailable"  # usage could not be read: an error, not a block
    OFF = "off"  # `claude.usage_gate: false`, or ANTHROPIC_API_KEY bills the API instead


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class UsageCheckInput(_Model):
    now: datetime  # the tick's `workflow.now()`, so the gate follows workflow time


class GateWindow(_Model):
    name: str
    utilization: float
    reserve: float
    resets_at: datetime
    full: bool = False


class UsageCheck(_Model):
    state: GateState
    windows: list[GateWindow] = Field(default_factory=list)
    error: str = ""  # UNAVAILABLE: why
    reason: str = ""  # OFF: why; FULL: which window and when it resets
    checked_at: datetime | None = None  # when the endpoint answered last
    next_call_at: datetime | None = None  # FULL: no call to the endpoint before this

    def held(self, now: datetime) -> bool:
        """Whether the tick starts nothing at `now`: a full window and no call due yet."""
        return self.state == GateState.FULL and self.next_call_at is not None and now < self.next_call_at


def reserves(claude: ClaudeSettings) -> dict[str, float]:
    """Return the reserve of each known window."""
    return {"five_hour": claude.reserve.five_hour, **dict.fromkeys(WEEKLY_WINDOWS, claude.reserve.weekly)}


def thresholds(claude: ClaudeSettings) -> dict[str, float]:
    """Utilization at which each window is full: `1 - reserve`."""
    return {name: 1 - reserve for name, reserve in reserves(claude).items()}


def gate_windows(usage: ClaudeUsage, claude: ClaudeSettings) -> list[GateWindow]:
    full = {w.name for w in usage.full(thresholds(claude))}
    held = reserves(claude)
    return [
        GateWindow(
            name=w.name,
            utilization=w.utilization,
            reserve=held.get(w.name, 0.0),
            resets_at=w.resets_at,
            full=w.name in full,
        )
        for w in usage.windows
    ]


def full_reason(windows: list[GateWindow]) -> str:
    """`five_hour 93% (reserve 10%) resets 2026-11-26 18:00 UTC`, for each full window."""
    return "; ".join(
        f"{w.name} {w.utilization:.0%} (reserve {w.reserve:.0%}) resets {w.resets_at.astimezone(UTC):%Y-%m-%d %H:%M} UTC"
        for w in windows
        if w.full
    )
