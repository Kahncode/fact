"""
Manual check: run Claude Code on a prompt in a temporary folder and print the result.

uv run python -m fact.worker.harnesses "reply with OK" --model haiku
"""

import argparse
import asyncio
import tempfile
from pathlib import Path

from fact.worker.harnesses.base import HarnessRequest
from fact.worker.harnesses.claude_code import ClaudeCodeHarness


async def _main(prompt: str, model: str | None, max_turns: int) -> int:
    with tempfile.TemporaryDirectory(prefix="fact-harness-") as tmp:
        folder = Path(tmp)
        transcript = folder / "transcript.jsonl"
        request = HarnessRequest(prompt=prompt, cwd=folder, model=model, max_turns=max_turns)
        result = await ClaudeCodeHarness().run(request, transcript)
        lines = len(transcript.read_text(encoding="utf-8").splitlines())
    print(f"session_id:    {result.session_id}")  # noqa: T201
    print(f"cost_usd:      {result.usage.cost_usd:.6f}")  # noqa: T201
    print(f"tokens in/out: {result.usage.tokens_in}/{result.usage.tokens_out}")  # noqa: T201
    print(f"stop_reason:   {result.stop_reason} (exit {result.exit_code}, turns {result.num_turns})")  # noqa: T201
    print(f"transcript:    {lines} lines")  # noqa: T201
    print(f"final_message: {result.final_message}")  # noqa: T201
    return 1 if result.is_error else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("prompt", nargs="?", default="reply with OK")
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-turns", type=int, default=2)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args.prompt, args.model, args.max_turns)))


if __name__ == "__main__":
    main()
