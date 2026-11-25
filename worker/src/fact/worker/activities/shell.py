"""`shell_step`: run a repository's own lint or test commands in the run's workspace, as a reported step."""

import asyncio
import contextlib
import time
from collections import deque
from pathlib import Path

from fact.core.activity_contract import SHELL_STEP, ShellStepInput
from fact.core.enums import StepKind, StepOutcome
from fact.core.project_config import ConfigError, RepoConfig, load_repo_config
from fact.core.types import SUMMARY_MAX_BYTES, StepResult, limit_text
from fact.worker.reporting import StepContext, StepReporter
from fact.worker.runners import ExecExit, ExecLine, Runner
from temporalio import activity
from temporalio.exceptions import ApplicationError

HEARTBEAT_S = 30.0
TAIL_BYTES = 4096  # tail of the failing command's output kept in `outputs["failure_output"]`
TAIL_LINES = TAIL_BYTES  # every line is at least one byte, so this keeps the last TAIL_BYTES
NOT_STARTED = 127
NL = "\n"


def _tail_bytes(text: str, max_bytes: int) -> str:
    return text.encode("utf-8")[-max_bytes:].decode("utf-8", errors="ignore")


async def _heartbeats(step: StepContext) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_S)
        if activity.in_activity():
            activity.heartbeat(f"{step.stage_id} running")


class ShellActivities:
    def __init__(self, runner: Runner, reporter: StepReporter) -> None:
        self._runner = runner
        self._reporter = reporter

    @activity.defn(name=SHELL_STEP)
    async def shell_step(self, spec: ShellStepInput) -> StepResult:
        async with self._reporter.step(spec.run_id, spec.stage_id, StepKind.SHELL, spec.attempt) as step:
            beat = asyncio.create_task(_heartbeats(step))
            try:
                await self._run_check(spec, step)
            finally:
                beat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await beat
        if step.result is None:
            msg = "step ended without a result"
            raise RuntimeError(msg)
        return step.result

    async def _run_check(self, spec: ShellStepInput, step: StepContext) -> None:
        worktree = self._worktree(spec)
        commands = getattr(self._load_config(worktree), spec.check)
        step.log(f"{spec.check} in {spec.repo}: {len(commands)} command(s)")
        deadline = time.monotonic() + spec.timeout_s
        for argv in commands:
            step.log(f"$ {' '.join(argv)}")
            tail: deque[str] = deque(maxlen=TAIL_LINES)
            code, timed_out = await self._exec(spec, argv, worktree, deadline, step, tail)
            if code != 0:
                reason = "timed out" if timed_out else f"exited with {code}"
                step.log(f"command {reason}")
                text = _tail_bytes(NL.join(tail), TAIL_BYTES)
                outputs = {"command": argv, "exit_code": code, "failure_output": text}
                header = f"`{' '.join(argv)}` {reason}" + NL
                header = limit_text(header, SUMMARY_MAX_BYTES // 2)
                summary = header + _tail_bytes(text, SUMMARY_MAX_BYTES - len(header.encode("utf-8")))
                step.finish(StepOutcome.FAILED_CHECKS, summary, outputs)
                return
        step.finish(StepOutcome.PASSED, outputs={"commands": len(commands)})

    def _worktree(self, spec: ShellStepInput) -> Path:
        path = spec.handle.repos.get(spec.repo)
        if path is None:
            msg = f"repository {spec.repo!r} is not in the workspace"
            raise ApplicationError(msg, non_retryable=True)
        return Path(path)

    @staticmethod
    def _load_config(worktree: Path) -> RepoConfig:
        try:
            return load_repo_config(worktree)  # the branch's own file, so its changes apply
        except ConfigError as exc:
            raise ApplicationError(str(exc), non_retryable=True) from exc

    async def _exec(  # noqa: PLR0913, PLR0917
        self,
        spec: ShellStepInput,
        argv: list[str],
        worktree: Path,
        deadline: float,
        step: StepContext,
        tail: deque[str],
    ) -> tuple[int, bool]:
        remaining = max(deadline - time.monotonic(), 0.0)
        code, timed_out = -1, False
        try:
            async for item in self._runner.exec(spec.handle, argv, cwd=worktree, timeout=remaining):
                if isinstance(item, ExecLine):
                    step.log(item.text)
                    tail.append(item.text)
                elif isinstance(item, ExecExit):
                    code, timed_out = item.exit_code, item.timed_out
        except OSError as exc:  # the program does not exist or cannot start: the check failed
            step.log(str(exc))
            tail.append(str(exc))
            return NOT_STARTED, False
        return code, timed_out
