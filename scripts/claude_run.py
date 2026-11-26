"""Run `claude -p` with stream-json output and collect the result, cost and usage windows (used by task_loop.py)."""

# ruff: noqa: INP001, S603
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from claude_log import format_line
from fact.core.claude_usage import LIMIT_TEXT_RE, ClaudeUsage, parse_rate_limit_info

BLOCKED_RE = re.compile(r"^\W*BLOCKED:[\s*_]*(.+)$", re.MULTILINE)
ROOT = Path(__file__).resolve().parents[1]
UNATTENDED_PROMPT = (
    "This session is fully unattended: nobody can answer questions or approve anything. "
    "Never ask the user anything and never wait for confirmation. Where the task or skill says to stop and ask, "
    "make the most conservative reasonable choice and note it in tasks/backlog.md. If you are truly blocked, "
    "do not commit; end with one line starting with 'BLOCKED:' that says why."
)


@dataclass
class RunResult:
    returncode: int = 0
    is_error: bool = False
    rate_limited: bool = False
    cost: float = 0.0
    usage: ClaudeUsage = field(default_factory=ClaudeUsage)
    text: str = ""

    @property
    def hit_limit(self) -> bool:
        """The run was refused, or some window reached 100%: its work may have been cut short."""
        return self.rate_limited or bool(self.usage.blocked(1.0))

    @property
    def blocker(self) -> str:
        """What a human must do, from the agent's final `BLOCKED: ...` line; empty when it did not say it was blocked."""
        found = BLOCKED_RE.findall(self.text)
        return found[-1].strip() if found else ""

    def failure_reason(self) -> str:
        if self.blocker:
            return f"BLOCKED: {self.blocker}"
        text = " ".join(self.text.split())
        last = text if len(text) <= 600 else "..." + text[-600:].partition(" ")[2]  # noqa: PLR2004
        return f"exit {self.returncode}{', error' if self.is_error else ''}: {last or 'no result from the agent'}"


def run_claude(  # noqa: PLR0913
    prompt: str,
    model: str,
    permission_mode: str,
    log_path: Path | None,
    *,
    unattended: bool = False,
    note: str = "",
) -> RunResult:
    """
    Run one prompt; `note` is appended to the system prompt (for example, how to resume partial work).

    With `log_path` (a .jsonl), the raw events go there and a readable transcript to the same path with `.log`.
    """
    cmd = [
        shutil.which("claude") or "claude",
        "-p",
        prompt,
        "--model",
        model,
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    if permission_mode:
        cmd += ["--permission-mode", permission_mode]
    system = " ".join(s for s in (UNATTENDED_PROMPT if unattended else "", note) if s)
    if system:
        cmd += ["--append-system-prompt", system]
    if unattended:
        cmd += ["--disallowedTools", "AskUserQuestion"]
    result = RunResult()
    log_file = log_path.open("w", encoding="utf-8") if log_path else None
    transcript = log_path.with_suffix(".log").open("w", encoding="utf-8") if log_path else None
    proc = subprocess.Popen(
        cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace"
    )
    assert proc.stdout is not None  # noqa: S101
    for line in proc.stdout:
        if log_file and transcript:
            log_file.write(line)
            if entry := format_line(line):
                transcript.write(entry + "\n\n")
                transcript.flush()
        handle_event(line, result)
    result.returncode = proc.wait()
    for f in (log_file, transcript):
        if f:
            f.close()
    return result


def handle_event(line: str, result: RunResult) -> None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return
    if event.get("type") == "rate_limit_event":
        info = event.get("rate_limit_info", {})
        result.usage = result.usage.merge(parse_rate_limit_info(info))
        if info.get("status") == "rejected":
            result.rate_limited = True
    elif event.get("type") == "result":
        result.is_error = bool(event.get("is_error"))
        result.cost = event.get("total_cost_usd", 0.0)
        result.text = str(event.get("result", ""))
        if result.is_error and LIMIT_TEXT_RE.search(result.text):
            result.rate_limited = True
