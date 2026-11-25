"""The harness contract: run a coding agent headless on a prompt in a folder and report what it did."""

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from fact.core.types import Usage
from pydantic import BaseModel, ConfigDict, Field

OnMessage = Callable[[dict[str, Any]], None]


class HarnessError(RuntimeError):
    """The harness could not start the agent at all (missing binary, bad request)."""


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class HarnessRequest(_Model):
    prompt: str
    cwd: Path
    model: str | None = None  # harness default when None
    allowed_tools: list[str] = Field(default_factory=list)  # empty: the harness's own permission rules apply
    max_turns: int | None = None
    permission_mode: str | None = None  # harness-specific, such as "auto" for Claude Code; None: the harness default
    output_schema: dict[str, Any] | None = None  # JSON Schema the agent's final answer must match
    resume_session_id: str | None = None
    env: dict[str, str] = Field(default_factory=dict)  # added to a minimal environment; agent credentials go here


class PermissionDenial(_Model):
    """A tool call the harness refused because it would have needed a person's approval."""

    tool_name: str
    tool_input: dict[str, Any] = Field(default_factory=dict)


class HarnessResult(_Model):
    session_id: str | None
    final_message: str
    is_error: bool
    exit_code: int
    stop_reason: str  # harness-specific, such as "success" or "error_max_turns" for Claude Code
    num_turns: int = 0
    usage: Usage = Field(default_factory=Usage)
    structured_output: dict[str, Any] | None = None  # the final answer matching `output_schema`, when one was asked
    permission_denials: list[PermissionDenial] = Field(default_factory=list)


class Harness(Protocol):
    name: str

    async def run(
        self,
        request: HarnessRequest,
        transcript_path: Path,
        on_message: OnMessage | None = None,
    ) -> HarnessResult:
        """
        Run the agent to completion, writing every raw message to `transcript_path`.

        `on_message` is called with each parsed message for progress. Cancelling the call kills the agent's whole
        process tree.
        """
        ...
