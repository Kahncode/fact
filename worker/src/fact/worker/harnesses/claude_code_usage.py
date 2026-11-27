"""
Claude Code's usage reports as `Usage`: from the `result` message, or summed from `assistant` messages.

Only what Claude Code reported: the stream carries tokens per message but no cost, so a cut-short session's cost
stays None.
"""

from collections.abc import Iterable
from typing import Any

from fact.core.types import Usage

_TOKEN_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
_INPUT_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
# `modelUsage` names of the `usage` keys
_MODEL_KEYS = {
    "inputTokens": "input_tokens",
    "cacheCreationInputTokens": "cache_creation_input_tokens",
    "cacheReadInputTokens": "cache_read_input_tokens",
    "outputTokens": "output_tokens",
}


class StreamUsage:
    """
    Usage of a session from its `assistant` messages: tokens for a session cut short, peak context for every session.

    Claude Code sends one `assistant` message per content block, all with the same `message.id` and usage, so the
    last usage seen per id counts once. Claude Code reports no cost per message, so the cost stays unknown (None).
    These are the counts the messages carried: `output_tokens` may be a snapshot from the start of a message and lower
    than its final count. With no `assistant` message the session never ran: `Usage()`, not marked partial.

    Peak context and turns count only the main session's messages: a subagent's carry a `parent_tool_use_id`.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, dict[str, Any]] = {}
        self._anonymous: list[dict[str, Any]] = []
        self._main_ids: set[str] = set()
        self._main_anonymous = 0
        self.peak_context: int | None = None

    def add(self, message: dict[str, Any]) -> None:
        inner = message.get("message")
        if message.get("type") != "assistant" or not isinstance(inner, dict):
            return
        usage = inner.get("usage")
        if not isinstance(usage, dict):
            return
        message_id = inner.get("id")
        if isinstance(message_id, str):
            self._by_id[message_id] = usage
        else:
            self._anonymous.append(usage)
        if message.get("parent_tool_use_id"):
            return
        if isinstance(message_id, str):
            self._main_ids.add(message_id)
        else:
            self._main_anonymous += 1
        context = sum(int(usage.get(key) or 0) for key in _INPUT_KEYS)
        self.peak_context = max(self.peak_context or 0, context)

    def usage(self) -> Usage:
        reported = [*self._by_id.values(), *self._anonymous]
        if not reported:
            return Usage()
        turns = len(self._main_ids) + self._main_anonymous
        return usage_from(_sum(reported), cost_usd=None, partial=True, turns=turns, peak_context=self.peak_context)


def _sum(usages: Iterable[dict[str, Any]]) -> dict[str, int]:
    totals: dict[str, int] = dict.fromkeys(_TOKEN_KEYS, 0)
    for u in usages:
        for key in _TOKEN_KEYS:
            totals[key] += int(u.get(key) or 0)
    return totals


def session_tokens(result: dict[str, Any]) -> dict[str, int]:
    """
    Tokens of the whole session from a `result` message, in `usage` keys.

    `modelUsage` (summed over its models) covers the whole session; `usage` covers only the last part of a session
    Claude Code woke again, so it is the fallback when `modelUsage` is missing.
    """
    models = result.get("modelUsage")
    if isinstance(models, dict) and (per_model := [m for m in models.values() if isinstance(m, dict)]):
        return _sum({usage_key: m.get(key) for key, usage_key in _MODEL_KEYS.items()} for m in per_model)
    u = result.get("usage")
    return _sum([u] if isinstance(u, dict) else [])


def usage_from(
    u: dict[str, Any],
    cost_usd: float | None,
    *,
    partial: bool = False,
    turns: int | None = None,
    peak_context: int | None = None,
) -> Usage:
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
        turns=turns,
        peak_context_tokens=peak_context,
    )
