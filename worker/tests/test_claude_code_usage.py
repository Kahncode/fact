"""Claude Code usage: a cost the result does not report stays None; a cut-short stream counts each message once."""

from pathlib import Path
from typing import Any

from fact.core.types import Usage
from fact.worker.harnesses.claude_code import parse_line, result_from_messages
from fact.worker.harnesses.claude_code_usage import StreamUsage

OK_STREAM = Path(__file__).parent / "fixtures" / "claude_stream_ok.jsonl"


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
    )


def test_a_stream_without_assistant_messages_reports_empty_usage() -> None:
    stream = StreamUsage()
    stream.add({"type": "system", "subtype": "init", "session_id": "s-1"})
    assert stream.usage() == Usage()
