"""Claude Code adapter: `claude -p --output-format stream-json`, prompt on stdin, every message to the transcript."""

import asyncio
import contextlib
import json
import shutil
from collections.abc import AsyncIterator, Iterable, Sequence
from pathlib import Path
from typing import Any

from fact.core.claude_usage import LIMIT_TEXT_RE, ClaudeUsage, parse_rate_limit_info
from fact.core.types import Usage
from fact.worker.harnesses.base import HarnessError, HarnessRequest, HarnessResult, OnMessage, PermissionDenial
from fact.worker.harnesses.claude_code_usage import StreamUsage, usage_from
from fact.worker.runners.process import build_env, kill_tree, spawn

_LINE_LIMIT = 1 << 26  # one stream-json line carries a whole tool result
_STDERR_TAIL = 4096
_CHUNK = 1 << 16
_DROPPED_LINE = '{"type":"fact_dropped_line","reason":"line too long"}'
_CMD_SPECIAL = frozenset('"&|<>^%!\r\n')
_STDERR_GRACE = 5.0  # seconds to finish reading stderr after the process tree is gone
# Runs must not read or write Claude Code's per-project auto-memory, which would carry notes between runs and projects.
_ISOLATION_ENV = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}


class _Collector:
    """
    Keeps what the result needs from the message stream.

    That is the session id, the last `result` message and the usage windows of every `rate_limit_event` (one with
    `status: rejected` means the usage limit refused the run).
    """

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.result: dict[str, Any] | None = None
        self.windows = ClaudeUsage()
        self.rejected = False
        self.stream = StreamUsage()

    def add(self, message: dict[str, Any]) -> None:
        if isinstance(sid := message.get("session_id"), str):
            self.session_id = sid
        self.stream.add(message)
        if message.get("type") == "result":
            self.result = message
        elif message.get("type") == "rate_limit_event" and isinstance(info := message.get("rate_limit_info"), dict):
            self.windows = self.windows.merge(parse_rate_limit_info(info))
            self.rejected = self.rejected or info.get("status") == "rejected"

    def build(self, exit_code: int, stderr: str = "") -> HarnessResult:
        result = self._build(exit_code, stderr)
        # only a run that failed was cut short: Claude Code may go on after a rejected event (extra usage)
        limited = result.is_error and (self.rejected or bool(LIMIT_TEXT_RE.search(result.final_message)))
        return result.model_copy(update={"usage_windows": self.windows, "rate_limited": limited})

    def _build(self, exit_code: int, stderr: str) -> HarnessResult:
        r = self.result
        if r is None:
            return HarnessResult(
                session_id=self.session_id,
                final_message=stderr.strip() or f"claude exited with code {exit_code} without a result message",
                is_error=True,
                exit_code=exit_code,
                stop_reason="no_result",
                usage=self.stream.usage(),
            )
        text = r.get("result")
        final = text if isinstance(text, str) else "\n".join(str(e) for e in r.get("errors") or [])
        return HarnessResult(
            session_id=r.get("session_id") or self.session_id,
            final_message=final,
            is_error=bool(r.get("is_error")) or exit_code != 0,
            exit_code=exit_code,
            stop_reason=str(r.get("subtype") or "unknown"),
            num_turns=int(r.get("num_turns") or 0),
            usage=_usage(r),
            structured_output=_structured(r),
            permission_denials=_denials(r),
        )


def _usage(result: dict[str, Any]) -> Usage:
    """Return the usage the `result` message reports; a cost it does not report stays None."""
    u = result.get("usage")
    cost = result.get("total_cost_usd")
    return usage_from(u if isinstance(u, dict) else {}, float(cost) if isinstance(cost, int | float) else None)


def _structured(result: dict[str, Any]) -> dict[str, Any] | None:
    """Return the final answer matching `--json-schema`, or None when none was asked or the agent never gave one."""
    found = result.get("structured_output")
    return found if isinstance(found, dict) else None


def _denials(result: dict[str, Any]) -> list[PermissionDenial]:
    denials = []
    for d in result.get("permission_denials") or []:
        if isinstance(d, dict) and isinstance(name := d.get("tool_name"), str):
            tool_input = d.get("tool_input")
            denials.append(
                PermissionDenial(tool_name=name, tool_input=tool_input if isinstance(tool_input, dict) else {})
            )
    return denials


def parse_line(line: str) -> dict[str, Any] | None:
    """One stream-json line as a dict, or None for blank or non-JSON lines."""
    try:
        message = json.loads(line)
    except ValueError:
        return None
    return message if isinstance(message, dict) else None


def result_from_messages(messages: Iterable[dict[str, Any]], exit_code: int, stderr: str = "") -> HarnessResult:
    collector = _Collector()
    for message in messages:
        collector.add(message)
    return collector.build(exit_code, stderr)


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace").removesuffix("\r")


async def _lines(stream: asyncio.StreamReader, limit: int) -> AsyncIterator[str]:
    """Split output into lines. A line over `limit` bytes is dropped whole and replaced by one marker line."""
    buf = bytearray()
    dropping = False  # inside an over-long line whose marker was already yielded
    while chunk := await stream.read(_CHUNK):
        buf += chunk
        while (nl := buf.find(b"\n")) >= 0:
            line = bytes(buf[:nl]).removesuffix(b"\r")
            del buf[: nl + 1]
            if dropping:
                dropping = False
            elif len(line) > limit:
                yield _DROPPED_LINE
            else:
                yield _decode(line)
        if len(buf) > limit and not dropping:
            yield _DROPPED_LINE
            dropping = True
        if dropping:
            buf.clear()
    if buf and not dropping:
        yield _decode(bytes(buf))


async def _tail(stream: asyncio.StreamReader) -> str:
    """Drain stderr so the process never blocks on it, and keep the last few KB for error reports."""
    kept = b""
    while chunk := await stream.read(65536):
        kept = (kept + chunk)[-_STDERR_TAIL:]
    return kept.decode("utf-8", errors="replace")


async def _send_prompt(proc: asyncio.subprocess.Process, prompt: str) -> None:
    assert proc.stdin is not None  # noqa: S101
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):  # the process may die before reading
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
    proc.stdin.close()


class ClaudeCodeHarness:
    """Runs Claude Code as a subprocess. `command` replaces the resolved `claude` binary (tests use a fake)."""

    name = "claude-code"

    def __init__(self, command: Sequence[str] | None = None) -> None:
        self._command = list(command) if command else None

    def _resolve(self) -> list[str]:
        if self._command:
            return self._command
        binary = shutil.which("claude")  # finds claude.cmd or claude.exe on Windows through PATHEXT
        if binary is None:
            msg = "claude not found on PATH; install Claude Code"
            raise HarnessError(msg)
        return [binary]

    @staticmethod
    def _check_batch_args(argv: list[str]) -> None:
        """Reject cmd.exe metacharacters: a `.cmd`/`.bat` re-parses `&|<>^%` and quotes that Python does not escape."""
        if Path(argv[0]).suffix.lower() not in {".cmd", ".bat"}:
            return
        for arg in argv[1:]:
            if any(c in _CMD_SPECIAL for c in arg):
                msg = f"argument {arg!r} is unsafe for {Path(argv[0]).name}; install the native claude binary"
                raise HarnessError(msg)

    def build_argv(self, request: HarnessRequest) -> list[str]:
        # Headless: nothing may wait on an approval; whatever would prompt is denied and listed in the result.
        argv = [*self._resolve(), "-p", "--output-format", "stream-json", "--verbose", "--permission-prompts", "none"]
        if request.permission_mode:
            argv += ["--permission-mode", request.permission_mode]
        if request.output_schema is not None:
            argv += ["--json-schema", json.dumps(request.output_schema, separators=(",", ":"))]
        if request.model:
            argv += ["--model", request.model]
        if request.max_turns is not None:
            argv += ["--max-turns", str(request.max_turns)]
        if request.resume_session_id:
            argv += ["--resume", request.resume_session_id]
        if request.allowed_tools:
            argv += ["--allowedTools", *request.allowed_tools]  # last: the flag takes several values
        self._check_batch_args(argv)
        return argv

    async def run(
        self,
        request: HarnessRequest,
        transcript_path: Path,
        on_message: OnMessage | None = None,
    ) -> HarnessResult:
        argv = self.build_argv(request)
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        env = build_env({**_ISOLATION_ENV, **(request.env or {})})
        try:
            proc = await spawn(argv, request.cwd, env, pipe_stdin=True, merge_stderr=False)
        except OSError as e:
            msg = f"cannot start {argv[0]} in {request.cwd}: {e}"
            raise HarnessError(msg) from e
        assert proc.stdout is not None  # noqa: S101
        assert proc.stderr is not None  # noqa: S101
        stderr_task = asyncio.create_task(_tail(proc.stderr))
        collector = _Collector()
        try:
            await _send_prompt(proc, request.prompt)
            with transcript_path.open("w", encoding="utf-8", newline="\n") as transcript:
                async for line in _lines(proc.stdout, _LINE_LIMIT):
                    transcript.write(line + "\n")
                    transcript.flush()  # a cancelled or crashed run still leaves its transcript
                    if (message := parse_line(line)) is not None:
                        collector.add(message)
                        if on_message is not None:
                            on_message(message)
            exit_code = await proc.wait()
        finally:
            await kill_tree(proc)  # also runs on cancellation
            await proc.wait()
            stderr = ""
            with contextlib.suppress(TimeoutError):  # an unreachable descendant can hold stderr open on Windows
                stderr = await asyncio.wait_for(stderr_task, _STDERR_GRACE)
        return collector.build(exit_code, stderr)
