"""
Claude Code usage: a cost the result does not report stays None; a cut-short stream counts each message once.

A woken session (two `result` messages) counts its whole session: turns over every `result`, tokens from the final
`result`'s `modelUsage`, peak context from the main session's `assistant` messages.
"""

from pathlib import Path
from typing import Any

from fact.core.types import Usage
from fact.worker.harnesses.claude_code import parse_line, result_from_messages
from fact.worker.harnesses.claude_code_usage import StreamUsage

FIXTURES = Path(__file__).parent / "fixtures"
OK_STREAM = FIXTURES / "claude_stream_ok.jsonl"
WOKEN_STREAM = FIXTURES / "claude_stream_woken.jsonl"  # error_max_turns (101 turns), woken again, success (9 turns)
WOKEN_PEAK = 4 + 50_000 + 6_000  # msg_c; the subagent's larger turn does not count


def _messages(path: Path) -> list[dict[str, Any]]:
    return [m for line in path.read_text(encoding="utf-8").splitlines() if (m := parse_line(line)) is not None]


def test_a_result_without_a_cost_reports_none_not_zero() -> None:
    messages = _messages(OK_STREAM)
    result_message = next(m for m in messages if m.get("type") == "result")
    del result_message["total_cost_usd"]
    assert result_from_messages(messages, exit_code=0).usage.cost_usd is None


def test_stream_usage_counts_each_message_once_and_reports_no_cost() -> None:
    def assistant(message_id: str | None, **usage: int) -> dict[str, Any]:
        inner: dict[str, Any] = {"usage": usage} | ({"id": message_id} if message_id else {})
        return {"type": "assistant", "message": inner}

    stream = StreamUsage()
    for message in [
        assistant("m1", input_tokens=3, output_tokens=1),  # first content block of m1
        assistant("m1", input_tokens=3, cache_read_input_tokens=50, output_tokens=9),  # its last block: the one kept
        assistant(None, input_tokens=1, cache_creation_input_tokens=7, output_tokens=2),
        {"type": "user", "message": {"usage": {"input_tokens": 999}}},
    ]:
        stream.add(message)
    assert stream.usage() == Usage(
        tokens_in=3 + 50 + 1 + 7,
        tokens_out=9 + 2,
        cache_read_tokens=50,
        cache_write_tokens=7,
        cost_usd=None,
        partial=True,
        turns=2,
        peak_context_tokens=3 + 50,
    )


def test_a_woken_session_counts_the_whole_session() -> None:
    result = result_from_messages(_messages(WOKEN_STREAM), exit_code=0)
    assert result.num_turns == 101 + 9
    assert [(p.stop_reason, p.num_turns, p.cost_usd) for p in result.parts] == [
        ("error_max_turns", 101, 1.5),
        ("success", 9, 2.3),
    ]
    assert (result.stop_reason, result.is_error, result.final_message) == ("success", False, "done")
    # the final result's modelUsage over both models, not its `usage` (the last part only)
    assert result.usage == Usage(
        tokens_in=(9 + 3) + (133_000 + 90_000) + (11_000 + 9_000),
        tokens_out=1_120 + 50,
        cache_read_tokens=133_000 + 90_000,
        cache_write_tokens=11_000 + 9_000,
        cost_usd=2.3,
        turns=110,
        peak_context_tokens=WOKEN_PEAK,
    )


def test_a_result_without_model_usage_falls_back_to_its_usage() -> None:
    messages = _messages(OK_STREAM)
    del next(m for m in messages if m.get("type") == "result")["modelUsage"]
    assert result_from_messages(messages, exit_code=0).usage.tokens_in == 2 + 19489 + 13121


def test_a_session_cut_short_keeps_its_peak_context() -> None:
    messages = [m for m in _messages(WOKEN_STREAM) if m.get("type") != "result"]
    usage = result_from_messages(messages, exit_code=1).usage
    assert (usage.partial, usage.turns, usage.peak_context_tokens) == (True, 3, WOKEN_PEAK)


def test_a_stream_without_assistant_messages_reports_empty_usage() -> None:
    stream = StreamUsage()
    stream.add({"type": "system", "subtype": "init", "session_id": "s-1"})
    assert stream.usage() == Usage()
