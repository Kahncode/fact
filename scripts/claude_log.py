"""Turn `claude -p --output-format stream-json` events into a readable transcript (used by claude_run.py)."""

# ruff: noqa: INP001
import json
from typing import Any

MAX_INPUT = 300  # characters of a tool call's input to show
MAX_RESULT = 800  # characters of a tool result to show
INPUT_KEYS = ("command", "file_path", "pattern", "path", "skill", "description", "prompt", "url")


def clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else f"{text[:limit]} ... [{len(text) - limit} more chars]"


def indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def tool_input(data: dict[str, Any]) -> str:
    """Return the input fields that say what a tool call does, or all of them when none is there."""
    shown = {k: data[k] for k in INPUT_KEYS if k in data} or data
    return clip(", ".join(f"{k}={v!r}" if not isinstance(v, str) else f"{k}={v}" for k, v in shown.items()), MAX_INPUT)


def result_text(content: object) -> str:
    if isinstance(content, list):
        return "\n".join(str(c.get("text", "")) if isinstance(c, dict) else str(c) for c in content)
    return str(content)


def format_blocks(role: str, blocks: list[Any]) -> list[str]:
    out: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and block.get("text", "").strip():
            out.append(f"{role}:\n{indent(block['text'].strip())}")
        elif kind == "tool_use":
            out.append(f"> {block.get('name')}: {tool_input(block.get('input') or {})}")
        elif kind == "tool_result":
            label = "< error" if block.get("is_error") else "<"
            out.append(f"{label}\n{indent(clip(result_text(block.get('content', '')), MAX_RESULT))}")
    return out


def format_event(event: dict[str, Any]) -> str | None:
    """One readable entry for an event, or None for events that only matter to the machine."""
    kind = event.get("type")
    if kind == "system" and event.get("subtype") == "init":
        return f"=== session {event.get('session_id')} model {event.get('model')} cwd {event.get('cwd')}"
    if kind in {"assistant", "user"}:
        content = (event.get("message") or {}).get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        lines = format_blocks("claude" if kind == "assistant" else "user", content or [])
        return "\n".join(lines) or None
    if kind == "rate_limit_event":
        info = event.get("rate_limit_info", {})
        return f"--- rate limit: {info.get('status')} ({info.get('rateLimitType', '')})"
    if kind == "result":
        cost = event.get("total_cost_usd", 0.0)
        head = f"=== result: {'error' if event.get('is_error') else 'ok'}, ${cost:.2f}, {event.get('num_turns')} turns"
        return f"{head}\n{indent(str(event.get('result', '')))}"
    return None


def format_line(line: str) -> str | None:
    """Format one stream-json line; non-JSON output (stderr) is kept as is."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return line.rstrip() or None
    return format_event(event) if isinstance(event, dict) else None
