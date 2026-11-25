"""agent_step with a fake harness against the real API app."""

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fact.core.enums import StepOutcome
from fact.core.settings import RoutingSettings, Settings
from fact.core.types import Route, RouteOverride, StepResult, Task, Usage, WorkspaceHandle
from fact.worker.activities import agent as agent_module
from fact.worker.activities.agent import AgentActivities, AgentStepInput
from fact.worker.harnesses import HarnessError, HarnessRequest, HarnessResult, OnMessage
from fact.worker.reporting import StepReporter
from httpx import AsyncClient
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

HOME = "acme/widget"
USAGE = Usage(tokens_in=1200, tokens_out=340, cost_usd=0.25)
MAX_TURNS = 7
TASK = Task(key="task:T-1", id="T-1", title="Add a thing", body="Do it", acceptance_criteria=["works"])
MESSAGES = [
    {"type": "system", "subtype": "init", "session_id": "s-1"},
    {"type": "assistant", "message": {"content": "working"}},
    {"type": "assistant", "message": {"content": "done"}},
]


class FakeHarness:
    """Writes `MESSAGES` to the transcript, optionally writes outputs, then returns `result` or raises `error`."""

    name = "fake"

    def __init__(
        self,
        result: HarnessResult | None = None,
        error: Exception | None = None,
        outputs: str | None = None,
        delay: float = 0.0,
    ) -> None:
        self.result = result or _result()
        self.error = error
        self.outputs = outputs
        self.delay = delay
        self.requests: list[HarnessRequest] = []

    async def run(
        self, request: HarnessRequest, transcript_path: Path, on_message: OnMessage | None = None
    ) -> HarnessResult:
        self.requests.append(request)
        with transcript_path.open("w", encoding="utf-8", newline="\n") as f:
            for m in MESSAGES:
                f.write(json.dumps(m) + "\n")
                if on_message is not None:
                    on_message(m)
        await asyncio.sleep(self.delay)
        if self.outputs is not None:
            out = request.cwd / ".fact-out"
            out.mkdir(exist_ok=True)
            (out / "implement.json").write_text(self.outputs, encoding="utf-8")
        if self.error is not None:
            raise self.error
        return self.result


def _result(stop_reason: str = "success", *, is_error: bool = False) -> HarnessResult:
    return HarnessResult(
        session_id="s-1",
        final_message="all done",
        is_error=is_error,
        exit_code=1 if is_error else 0,
        stop_reason=stop_reason,
        num_turns=2,
        usage=USAGE,
    )


@pytest.fixture
def handle(tmp_path: Path, run_id: str) -> WorkspaceHandle:
    root = tmp_path / "ws"
    prompts = root / "widget" / ".fact" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "implement.md").write_text("Implement $task_id: $task_title in $workspace\n$context", encoding="utf-8")
    return WorkspaceHandle(run_id=run_id, root=str(root), repos={HOME: str(root / "widget")})


@pytest.fixture
def agent_settings(settings: Settings) -> Settings:
    routing = RoutingSettings(
        default=Route(harness="fake", model="sonnet"), rules={"complex.implement": RouteOverride(model="opus")}
    )
    return settings.model_copy(update={"routing": routing})


@dataclass
class Env:
    """One agent_step test: the API client, settings and a workspace whose home repo has `implement.md`."""

    client: AsyncClient
    tmp_path: Path
    settings: Settings
    handle: WorkspaceHandle

    def activities(self, harness: FakeHarness, settings: Settings | None = None) -> AgentActivities:
        reporter = StepReporter("http://test", self.client, log_dir=self.tmp_path / "logs")
        return AgentActivities({harness.name: harness}, reporter, settings or self.settings)

    def input(self, **extra: Any) -> AgentStepInput:  # noqa: ANN401
        values = {
            "run_id": self.handle.run_id,
            "stage_id": "implement",
            "attempt": 1,
            "handle": self.handle,
            "task": TASK,
            "prompt_name": "implement",
            "context": "ctx here",
            "complexity": "complex",
        }
        return AgentStepInput.model_validate(values | extra)

    async def run(self, harness: FakeHarness, settings: Settings | None = None, **extra: object) -> StepResult:
        return await ActivityEnvironment().run(self.activities(harness, settings).agent_step, self.input(**extra))

    async def step(self) -> dict[str, Any]:
        (step,) = (await self.client.get(f"/api/v1/runs/{self.handle.run_id}")).json()["steps"]
        return step

    @property
    def out_file(self) -> Path:
        return Path(self.handle.root) / ".fact-out" / "implement.json"


@pytest.fixture
def env(client: AsyncClient, tmp_path: Path, agent_settings: Settings, handle: WorkspaceHandle) -> Env:
    return Env(client, tmp_path, agent_settings, handle)


async def test_success_records_harness_model_usage_and_transcript(env: Env) -> None:
    harness = FakeHarness(outputs='{"pr_title": "Add a thing"}')
    result = await env.run(harness)

    assert result.outcome == StepOutcome.PASSED
    assert result.summary == "all done"
    assert result.outputs == {"pr_title": "Add a thing"}
    assert result.usage == USAGE
    assert not env.out_file.exists()

    (request,) = harness.requests
    assert request.cwd == Path(env.handle.root)
    assert request.model == "opus"
    assert request.max_turns == env.settings.step_defaults.agent.max_turns
    assert request.prompt == f"Implement T-1: Add a thing in {env.handle.root}\nctx here"

    step = await env.step()
    assert step["kind"] == "agent"
    assert step["harness"] == "fake"
    assert step["model"] == "opus"
    assert step["tokens_in"] == USAGE.tokens_in
    assert step["tokens_out"] == USAGE.tokens_out
    assert float(step["cost_usd"]) == USAGE.cost_usd
    assert step["outputs"] == {"pr_title": "Add a thing"}
    assert step["transcript_ref"] == f"runs/{env.handle.run_id}/implement/1/transcript.jsonl"
    assert result.transcript_ref == step["transcript_ref"]
    transcript = (await env.client.get(f"/api/v1/artifacts/{step['transcript_ref']}")).text
    assert [json.loads(line) for line in transcript.splitlines()] == MESSAGES


async def test_harness_error_result_is_failed_error(env: Env) -> None:
    result = await env.run(FakeHarness(_result("error_during_execution", is_error=True)), complexity="standard")
    assert result.outcome == StepOutcome.FAILED_ERROR
    step = await env.step()
    assert step["outcome"] == "failed_error"
    assert step["model"] == "sonnet"
    assert step["transcript_ref"] is not None


async def test_harness_that_cannot_start_is_failed_error(env: Env) -> None:
    result = await env.run(FakeHarness(error=HarnessError("claude not found")))
    assert result.outcome == StepOutcome.FAILED_ERROR
    assert "claude not found" in result.summary
    assert (await env.step())["transcript_ref"] is not None  # what was written is kept


async def test_max_turns_is_stopped_by_monitor(env: Env) -> None:
    defaults = env.settings.step_defaults
    agent = defaults.agent.model_copy(update={"max_turns": MAX_TURNS})
    settings = env.settings.model_copy(update={"step_defaults": defaults.model_copy(update={"agent": agent})})
    harness = FakeHarness(_result("error_max_turns", is_error=True))
    result = await env.run(harness, settings)
    assert result.outcome == StepOutcome.STOPPED_BY_MONITOR
    assert harness.requests[0].max_turns == MAX_TURNS
    assert (await env.step())["outcome"] == "stopped_by_monitor"


async def test_invalid_outputs_file_fails_the_step(env: Env) -> None:
    result = await env.run(FakeHarness(outputs="[1, 2]"))
    assert result.outcome == StepOutcome.FAILED_ERROR
    assert "JSON object" in result.summary
    assert not env.out_file.exists()


async def test_oversized_outputs_fail_the_step(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_module, "OUTPUTS_MAX_BYTES", 10)
    result = await env.run(FakeHarness(outputs='{"big": "0123456789"}'))
    assert result.outcome == StepOutcome.FAILED_ERROR
    assert "artifact" in result.summary
    assert result.outputs == {}


async def test_stale_outputs_are_removed_before_the_run(env: Env) -> None:
    env.out_file.parent.mkdir()
    env.out_file.write_text('{"old": true}', encoding="utf-8")
    assert (await env.run(FakeHarness())).outputs == {}


async def test_missing_prompt_is_non_retryable(env: Env) -> None:
    with pytest.raises(ApplicationError, match="not found") as info:
        await env.run(FakeHarness(), prompt_name="absent")
    assert info.value.non_retryable
    assert (await env.step())["outcome"] == "failed_error"


@pytest.mark.parametrize(
    ("extra", "error"),
    [
        ({"override": RouteOverride(harness="nope")}, "not available"),
        ({"home_repo": "acme/missing"}, "home repository"),
    ],
)
async def test_bad_setup_is_non_retryable(env: Env, extra: dict[str, Any], error: str) -> None:
    with pytest.raises(ApplicationError, match=error) as info:
        await env.run(FakeHarness(), **extra)
    assert info.value.non_retryable


async def test_home_repo_selects_the_prompt_source(env: Env) -> None:
    other = Path(env.handle.root) / "other"
    (other / ".fact" / "prompts").mkdir(parents=True)
    (other / ".fact" / "prompts" / "implement.md").write_text("from other", encoding="utf-8")
    env.handle = env.handle.model_copy(update={"repos": {HOME: env.handle.repos[HOME], "acme/other": str(other)}})
    harness = FakeHarness()
    await env.run(harness, home_repo="acme/other")
    assert harness.requests[0].prompt == "from other"


async def test_heartbeats_carry_turn_count(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_module, "HEARTBEAT_S", 0.05)
    activity_env = ActivityEnvironment()
    beats: list[tuple[Any, ...]] = []
    activity_env.on_heartbeat = lambda *details: beats.append(details)
    await activity_env.run(env.activities(FakeHarness(delay=0.3)).agent_step, env.input())
    assert beats
    assert beats[-1] == ("implement: 2 turns",)


async def test_cancel_keeps_the_transcript(env: Env) -> None:
    activity_env = ActivityEnvironment()
    task = asyncio.create_task(activity_env.run(env.activities(FakeHarness(delay=30)).agent_step, env.input()))
    await asyncio.sleep(0.3)
    activity_env.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    step = await env.step()
    assert step["outcome"] == "failed_error"
    assert step["transcript_ref"] == f"runs/{env.handle.run_id}/implement/1/transcript.jsonl"


def test_turns_count_distinct_message_ids() -> None:
    progress = agent_module._Progress()  # noqa: SLF001
    for message_id in ("a", "a", "b", None, None):
        progress.on_message({"type": "assistant", "message": {"id": message_id}})
    progress.on_message({"type": "user"})
    assert progress.turns == 4  # noqa: PLR2004
