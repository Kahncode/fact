"""
The scheduler's usage gate in the API process: calls the undocumented usage endpoint only when it must.

`UsageGate.check(now)` is called by a tick that has a task to start and a free slot:

- gate off (`claude.usage_gate: false`, or ANTHROPIC_API_KEY set in the API's environment): no call;
- last answer had a full window and its `next_call_at` is ahead: no call, still full;
- else one call. A full window sets `next_call_at` to its reset plus `RESET_MARGIN`; a window still full once that
  has passed backs off `MIN_WAIT * stalls` (as the retired task loop's `wait_for_quota` did).

It reads the local Claude Code login: the API runs on the machine whose subscription the runs use. An API restart
drops the cached answer, which costs one call. `last` is what `GET /scheduler/preview` shows.
"""

import asyncio
import os
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta

from fact.core.claude_usage import ClaudeUsage, UsageUnavailableError, fetch_claude_usage, read_cli_token
from fact.core.settings import ClaudeSettings
from fact.core.usage_gate import GateState, UsageCheck, full_reason, gate_windows

RESET_MARGIN = timedelta(seconds=90)
MIN_WAIT = timedelta(minutes=5)
API_KEY_VAR = "ANTHROPIC_API_KEY"  # pragma: allowlist secret


def read_usage() -> ClaudeUsage:
    """Read the local Claude Code login's usage (blocking)."""
    return fetch_claude_usage(read_cli_token())


class UsageGate:
    def __init__(
        self,
        claude: ClaudeSettings,
        fetch: Callable[[], ClaudeUsage] = read_usage,
        environ: Mapping[str, str] = os.environ,
    ) -> None:
        self._claude = claude
        self._fetch = fetch
        self._environ = environ
        self._stalls = 0
        self.last: UsageCheck | None = None

    def _off_reason(self) -> str:
        if not self._claude.usage_gate:
            return "claude.usage_gate is false"
        if self._environ.get(API_KEY_VAR):
            return f"{API_KEY_VAR} is set: runs bill the API, not the subscription"
        return ""

    async def check(self, now: datetime) -> UsageCheck:
        if reason := self._off_reason():
            self.last = UsageCheck(state=GateState.OFF, reason=reason)
            return self.last
        if self.last is not None and self.last.held(now):
            return self.last
        try:
            usage = await asyncio.to_thread(self._fetch)
        except UsageUnavailableError as exc:
            self.last = UsageCheck(state=GateState.UNAVAILABLE, error=str(exc), checked_at=now)
            return self.last
        self.last = self._answer(usage, now)
        return self.last

    def _answer(self, usage: ClaudeUsage, now: datetime) -> UsageCheck:
        windows = gate_windows(usage, self._claude)
        full = [w for w in windows if w.full]
        if not full:
            self._stalls = 0
            return UsageCheck(state=GateState.OK, windows=windows, checked_at=now)
        resume = max(w.resets_at for w in full) + RESET_MARGIN
        if resume > now:
            self._stalls = 0
        else:  # still full past its reset: back off, longer each time
            self._stalls += 1
            resume = now + MIN_WAIT * self._stalls
        return UsageCheck(
            state=GateState.FULL, windows=windows, reason=full_reason(windows), checked_at=now, next_call_at=resume
        )
