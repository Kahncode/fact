"""
Claude Code's usage reports as `Usage`: from the `result` message, or summed from `assistant` messages.

Only what Claude Code reported: the stream carries tokens per message but no cost, so a cut-short session's cost
stays None.
"""

from collections.abc import Iterable
from typing import Any

from fact.core.types import Usage

_TOKEN_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


class StreamUsage:
    """
    Usage of a session from its `assistant` messages, for a session cut short before its `result` message.

    Claude Code sends one `assistant` message per content block, all with the same `message.id` and usage, so the
    last usage seen per id counts once. Claude Code reports no cost per message, so the cost stays unknown (None).
    These are the counts the messages carried: `output_tokens` may be a snapshot from the start of a message and lower
    than its final count. With no `assistant` message the session never ran: `Usage()`, not marked partial.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, dict[str, Any]] = {}
        self._anonymous: list[dict[str, Any]] = []

    def add(self, message: dict[str, Any]) -> None:
        inner = message.get("message")
        if message.get("type") != "assistant" or not isinstance(inner, dict):
            return
        usage = inner.get("usage")
        if not isinstance(usage, dict):
            return
        if isinstance(message_id := inner.get("id"), str):
            self._by_id[message_id] = usage
        else:
            self._anonymous.append(usage)

    def usage(self) -> Usage:
        reported = [*self._by_id.values(), *self._anonymous]
        if not reported:
            return Usage()
        return usage_from(_sum(reported), cost_usd=None, partial=True)


def _sum(usages: Iterable[dict[str, Any]]) -> dict[str, int]:
    totals: dict[str, int] = dict.fromkeys(_TOKEN_KEYS, 0)
    for u in usages:
        for key in _TOKEN_KEYS:
            totals[key] += int(u.get(key) or 0)
    return totals


def usage_from(u: dict[str, Any], cost_usd: float | None, *, partial: bool = False) -> Usage:
    """`tokens_in` counts every prompt token the model read: fresh input plus cache writes and cache reads."""
    cache_read = int(u.get("cache_read_input_tokens") or 0)
    cache_write = int(u.get("cache_creation_input_tokens") or 0)
    return Usage(
        tokens_in=int(u.get("input_tokens") or 0) + cache_read + cache_write,
        tokens_out=int(u.get("output_tokens") or 0),
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cost_usd=cost_usd,
        partial=partial,
    )
