"""The failure context a fix session gets: what failed (a check or a CI job), how, and the end of its output."""

from collections.abc import Sequence

from fact.core.activity_contract import CheckResult
from fact.core.types import PRRef, StepResult, limit_text

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


def ci_failure_context(failed: Sequence[tuple[PRRef, StepResult]]) -> str:
    """Build the text a CI fix session gets: each failed job of each PR with its link and the end of its log."""
    parts: list[str] = []
    for pr, step in failed:
        jobs = step.outputs.get("failures")
        if not isinstance(jobs, list) or not jobs:
            parts.append(f"### {pr.repo}#{pr.number}: {step.outcome}\n\n{step.summary}")
            continue
        for job in jobs:
            name, conclusion, url, tail = (job.get(k, "") for k in ("name", "conclusion", "url", "log_tail"))
            where = f"{pr.repo}#{pr.number} at {pr.head_sha[:12]}"
            parts.append(f"### {name}: {conclusion} on {where}\n\n{url}\n\nEnd of the log:\n\n```\n{tail}\n```")
    return limit_text("\n\n".join(parts), CONTEXT_MAX_BYTES)
