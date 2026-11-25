"""The failure context a fix session gets: what failed, with which command, and the end of its output."""

from collections.abc import Sequence

from fact.core.activity_contract import CheckResult
from fact.core.types import StepResult, limit_text

CONTEXT_MAX_BYTES = 32 * 1024  # failure context handed to a fix session


def failure_context(failures: Sequence[tuple[CheckResult, StepResult]]) -> str:
    """Build the text a fix session gets: each failed check with its command, exit code and output tail."""
    parts: list[str] = []
    for check, step in failures:
        command, code, output = (step.outputs.get(k) for k in ("command", "exit_code", "failure_output"))
        if isinstance(command, list) and isinstance(output, str):
            argv = " ".join(str(a) for a in command)
            parts.append(f"### {check.name}: `{argv}` exited with {code}\n\n```\n{output}\n```")
        else:
            parts.append(f"### {check.name}: {check.outcome}\n\n{step.summary}")
    return limit_text("\n\n".join(parts), CONTEXT_MAX_BYTES)
