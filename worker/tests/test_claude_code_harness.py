"""ClaudeCodeHarness: parsing recorded stream-json fixtures, argv, and running a fake `claude` end to end."""

import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
from fact.worker.harnesses import ClaudeCodeHarness, HarnessError, HarnessRequest, PermissionDenial, claude_code
from fact.worker.harnesses.claude_code import parse_line, result_from_messages

FIXTURES = Path(__file__).parent / "fixtures"
OK_STREAM = FIXTURES / "claude_stream_ok.jsonl"
ERROR_STREAM = FIXTURES / "claude_stream_error.jsonl"
FAKE = [sys.executable, str(FIXTURES / "fake_claude.py")]

# values read from the recorded fixtures
OK_TOKENS_OUT = 4
ERROR_TURNS = 2
ERROR_TOKENS_OUT = 395


def _messages(path: Path) -> list[dict[str, Any]]:
    return [m for line in path.read_text(encoding="utf-8").splitlines() if (m := parse_line(line)) is not None]


def test_ok_fixture_parses_into_result() -> None:
    result = result_from_messages(_messages(OK_STREAM), exit_code=0)
    assert result.session_id == "80ea6d1c-fe93-46a2-866a-e0872757af8d"
    assert result.final_message == "OK"
    assert result.is_error is False
    assert result.exit_code == 0
    assert result.stop_reason == "success"
    assert result.num_turns == 1
    assert result.usage.tokens_in == 2 + 19489 + 13121
    assert result.usage.tokens_out == OK_TOKENS_OUT
    assert result.usage.cost_usd == pytest.approx(0.00403121)


def test_max_turns_fixture_is_an_error() -> None:
    result = result_from_messages(_messages(ERROR_STREAM), exit_code=1)
    assert result.session_id == "749b4c38-e9de-4f67-9859-39e5efd8c640"
    assert result.is_error is True
    assert result.exit_code == 1
    assert result.stop_reason == "error_max_turns"
    assert result.num_turns == ERROR_TURNS
    assert "Reached maximum number of turns" in result.final_message
    assert result.usage.cost_usd == pytest.approx(0.00427571)
    assert result.usage.tokens_out == ERROR_TOKENS_OUT


def test_nonzero_exit_is_an_error_even_with_success_result() -> None:
    assert result_from_messages(_messages(OK_STREAM), exit_code=2).is_error is True


def test_missing_result_reports_stderr_and_session() -> None:
    init = _messages(OK_STREAM)[0]
    result = result_from_messages([init], exit_code=1, stderr="Error: not logged in\n")
    assert result.is_error is True
    assert result.stop_reason == "no_result"
    assert result.session_id == init["session_id"]
    assert result.final_message == "Error: not logged in"
    assert result.usage.cost_usd == 0.0


@pytest.mark.parametrize("line", ["", "not json", "[1, 2]", "Warning: something"])
def test_parse_line_skips_non_messages(line: str) -> None:
    assert parse_line(line) is None


def test_argv_has_flags_and_never_the_prompt(tmp_path: Path) -> None:
    request = HarnessRequest(
        prompt="do the thing",
        cwd=tmp_path,
        model="sonnet",
        max_turns=7,
        resume_session_id="abc",
        allowed_tools=["Read", "Bash(git status:*)"],
    )
    argv = ClaudeCodeHarness(command=["claude"]).build_argv(request)
    assert argv[:7] == ["claude", "-p", "--output-format", "stream-json", "--verbose", "--permission-prompts", "none"]
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--max-turns") + 1] == "7"
    assert argv[argv.index("--resume") + 1] == "abc"
    assert argv[argv.index("--allowedTools") :] == ["--allowedTools", "Read", "Bash(git status:*)"]
    assert "do the thing" not in argv


def test_argv_passes_permission_mode_and_schema(tmp_path: Path) -> None:
    schema = {"type": "object", "properties": {"status": {"type": "string"}}, "required": ["status"]}
    request = HarnessRequest(prompt="x", cwd=tmp_path, permission_mode="auto", output_schema=schema)
    argv = ClaudeCodeHarness(command=["claude"]).build_argv(request)
    assert argv[argv.index("--permission-mode") + 1] == "auto"
    assert json.loads(argv[argv.index("--json-schema") + 1]) == schema


def test_argv_omits_unset_options(tmp_path: Path) -> None:
    argv = ClaudeCodeHarness(command=["claude"]).build_argv(HarnessRequest(prompt="x", cwd=tmp_path))
    for flag in ("--model", "--max-turns", "--resume", "--allowedTools", "--permission-mode", "--json-schema"):
        assert flag not in argv


def test_result_carries_structured_output_and_denials() -> None:
    """Shaped like a real `claude -p --json-schema --permission-prompts none` result whose Write was refused."""
    answer = {"status": "blocked", "reason": "Write to probe.txt was denied"}
    result_message = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": "s-1",
        "result": json.dumps(answer),
        "structured_output": answer,
        "permission_denials": [
            {"tool_name": "Write", "tool_use_id": "toolu_1", "tool_input": {"file_path": "probe.txt", "content": "x"}},
            {"tool_use_id": "toolu_2"},  # no tool name: skipped
        ],
    }
    result = result_from_messages([result_message], exit_code=0)
    assert result.structured_output == answer
    assert result.permission_denials == [
        PermissionDenial(tool_name="Write", tool_input={"file_path": "probe.txt", "content": "x"})
    ]


def test_result_without_structured_output_has_none() -> None:
    result = result_from_messages(_messages(OK_STREAM), exit_code=0)
    assert result.structured_output is None
    assert result.permission_denials == []


def test_missing_binary_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(HarnessError, match="claude not found"):
        ClaudeCodeHarness().build_argv(HarnessRequest(prompt="x", cwd=tmp_path))


async def test_run_streams_transcript_and_messages(tmp_path: Path) -> None:
    record = tmp_path / "record.json"
    prompt = "reply with OK\n" + "x" * 50_000  # longer than a Windows command line
    env = {"FAKE_STREAM": str(OK_STREAM), "FAKE_RECORD": str(record), "FAKE_STDERR": "noise on stderr\n"}
    request = HarnessRequest(prompt=prompt, cwd=tmp_path, model="haiku", max_turns=2, env=env)
    transcript = tmp_path / "out" / "transcript.jsonl"
    seen: list[dict[str, Any]] = []

    result = await ClaudeCodeHarness(command=FAKE).run(request, transcript, seen.append)

    assert result.final_message == "OK"
    assert result.is_error is False
    assert result.session_id == "80ea6d1c-fe93-46a2-866a-e0872757af8d"
    assert transcript.read_text(encoding="utf-8") == OK_STREAM.read_text(encoding="utf-8")
    assert [m["type"] for m in seen] == ["system", "assistant", "rate_limit_event", "result"]
    recorded = json.loads(record.read_text(encoding="utf-8"))
    assert recorded["prompt"] == prompt
    assert recorded["argv"][:4] == ["-p", "--output-format", "stream-json", "--verbose"]


async def test_run_reports_max_turns_error(tmp_path: Path) -> None:
    env = {"FAKE_STREAM": str(ERROR_STREAM), "FAKE_EXIT": "1"}
    request = HarnessRequest(prompt="go", cwd=tmp_path, env=env)
    result = await ClaudeCodeHarness(command=FAKE).run(request, tmp_path / "t.jsonl")
    assert result.is_error is True
    assert result.exit_code == 1
    assert result.stop_reason == "error_max_turns"


async def test_run_without_result_uses_stderr(tmp_path: Path) -> None:
    request = HarnessRequest(prompt="go", cwd=tmp_path, env={"FAKE_STDERR": "Invalid API key\n", "FAKE_EXIT": "1"})
    result = await ClaudeCodeHarness(command=FAKE).run(request, tmp_path / "t.jsonl")
    assert result.stop_reason == "no_result"
    assert result.final_message == "Invalid API key"
    assert result.exit_code == 1


async def test_cancel_kills_process_and_keeps_transcript(tmp_path: Path) -> None:
    request = HarnessRequest(prompt="go", cwd=tmp_path, env={"FAKE_STREAM": str(OK_STREAM), "FAKE_HANG": "1"})
    transcript = tmp_path / "t.jsonl"
    started = asyncio.Event()
    task = asyncio.create_task(
        ClaudeCodeHarness(command=FAKE).run(request, transcript, lambda _m: started.set()),
    )
    await asyncio.wait_for(started.wait(), 30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 30)  # returns only once the fake process has been killed and reaped
    assert transcript.read_text(encoding="utf-8").splitlines() == OK_STREAM.read_text(encoding="utf-8").splitlines()[:1]


@pytest.mark.parametrize("tools", [["Bash(make&calc)"], ["Bash(echo %PATH%)"], ['Bash("x")']])
def test_batch_wrapper_rejects_cmd_special_characters(tmp_path: Path, tools: list[str]) -> None:
    harness = ClaudeCodeHarness(command=[str(tmp_path / "claude.cmd")])
    with pytest.raises(HarnessError, match="unsafe"):
        harness.build_argv(HarnessRequest(prompt="x", cwd=tmp_path, allowed_tools=tools))


def test_native_binary_allows_any_argument(tmp_path: Path) -> None:
    harness = ClaudeCodeHarness(command=[str(tmp_path / "claude.exe")])
    argv = harness.build_argv(HarnessRequest(prompt="x", cwd=tmp_path, allowed_tools=["Bash(a&b)"]))
    assert argv[-1] == "Bash(a&b)"


async def test_missing_cwd_raises_harness_error(tmp_path: Path) -> None:
    request = HarnessRequest(prompt="x", cwd=tmp_path / "missing")
    with pytest.raises(HarnessError, match="cannot start"):
        await ClaudeCodeHarness(command=FAKE).run(request, tmp_path / "t.jsonl")


async def test_process_that_ignores_stdin_is_not_a_crash(tmp_path: Path) -> None:
    request = HarnessRequest(prompt="x" * 4_000_000, cwd=tmp_path, env={"FAKE_SKIP_STDIN": "1"})
    result = await ClaudeCodeHarness(command=FAKE).run(request, tmp_path / "t.jsonl")
    assert result.stop_reason == "no_result"
    assert result.exit_code == 1


async def test_overlong_line_is_dropped_and_stream_continues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    longest = max(len(line.encode("utf-8")) for line in OK_STREAM.read_text(encoding="utf-8").splitlines())
    monkeypatch.setattr(claude_code, "_LINE_LIMIT", longest)
    request = HarnessRequest(
        prompt="go", cwd=tmp_path, env={"FAKE_STREAM": str(OK_STREAM), "FAKE_LONG_LINE": str(longest * 3 + 1)}
    )
    transcript = tmp_path / "t.jsonl"
    result = await ClaudeCodeHarness(command=FAKE).run(request, transcript)
    lines = transcript.read_text(encoding="utf-8").splitlines()
    assert lines == [claude_code._DROPPED_LINE, *OK_STREAM.read_text(encoding="utf-8").splitlines()]  # noqa: SLF001
    assert result.final_message == "OK"
