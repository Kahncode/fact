"""agent_step with a fake harness against the real API app."""

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fact.core.activity_contract import AGENT_INTERRUPTED
from fact.core.enums import StepOutcome
from fact.core.settings import RoutingSettings, Settings
from fact.core.types import Route, RouteOverride, StepResult, Task, Usage, WorkspaceHandle
from fact.worker.activities import agent as agent_module
from fact.worker.activities.agent import AgentActivities, AgentStepInput
from fact.worker.harnesses import HarnessError, HarnessRequest, HarnessResult, OnMessage, PermissionDenial
from fact.worker.prompts import BASE_OUTPUT_SCHEMA
from fact.worker.reporting import StepReporter
from httpx import AsyncClient
from temporalio.activity import ActivityCancellationDetails
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
    """Writes `MESSAGES` to the transcript, then returns `result` or raises `error`."""

    name = "fake"

    def __init__(
        self,
        result: HarnessResult | None = None,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.result = result or _result()
        self.error = error
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
        if self.error is not None:
            raise self.error
        return self.result


DONE = {"status": "done", "summary": "Added the thing.", "title": "feat(T-1): add a thing"}


def _result(
    stop_reason: str = "success",
    *,
    is_error: bool = False,
    answer: dict[str, Any] | None = DONE,
    denials: tuple[PermissionDenial, ...] = (),
) -> HarnessResult:
    return HarnessResult(
        session_id="s-1",
        final_message="all done",
        is_error=is_error,
        exit_code=1 if is_error else 0,
        stop_reason=stop_reason,
        num_turns=2,
        usage=USAGE,
        structured_output=answer,
        permission_denials=list(denials),
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


@pytest.fixture
def env(client: AsyncClient, tmp_path: Path, agent_settings: Settings, handle: WorkspaceHandle) -> Env:
    return Env(client, tmp_path, agent_settings, handle)


async def test_success_records_harness_model_usage_and_transcript(env: Env) -> None:
    harness = FakeHarness()
    result = await env.run(harness)

    assert result.outcome == StepOutcome.PASSED
    assert result.summary == "Added the thing."
    assert result.outputs == DONE
    assert result.usage == USAGE

    (request,) = harness.requests
    assert request.cwd == Path(env.handle.root)
    assert request.model == "opus"
    assert request.max_turns == env.settings.step_defaults.agent.max_turns
    assert request.permission_mode == "auto"
    assert request.output_schema == BASE_OUTPUT_SCHEMA
    assert request.prompt == f"Implement T-1: Add a thing in {env.handle.root}\nctx here"

    step = await env.step()
    assert step["kind"] == "agent"
    assert step["harness"] == "fake"
    assert step["model"] == "opus"
    assert step["tokens_in"] == USAGE.tokens_in
    assert step["tokens_out"] == USAGE.tokens_out
    assert float(step["cost_usd"]) == USAGE.cost_usd
    assert step["outputs"] == DONE
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


async def test_no_structured_answer_fails_the_step(env: Env) -> None:
    result = await env.run(FakeHarness(_result(answer=None)))
    assert result.outcome == StepOutcome.FAILED_ERROR
    assert "without a structured answer" in result.summary
    assert "all done" in result.summary


async def test_blocked_answer_needs_a_human_and_names_denied_tools(env: Env) -> None:
    answer = {"status": "blocked", "summary": "Nothing changed.", "blocker": "Writes were refused."}
    denial = PermissionDenial(tool_name="Write", tool_input={"file_path": "x.py"})
    result = await env.run(FakeHarness(_result(answer=answer, denials=(denial, denial))))
    assert result.outcome == StepOutcome.NEEDS_HUMAN
    assert result.summary == "Writes were refused.\n\n2 tool calls denied (Write)"
    assert result.outputs["status"] == "blocked"
    assert result.outputs["permission_denials"] == [denial.model_dump()] * 2
    step = await env.step()
    assert step["outcome"] == "needs_human"
    log = (await env.client.get(f"/api/v1/artifacts/{result.log_ref}")).text
    assert 'permission denied: Write {"file_path": "x.py"}' in log


async def test_done_answer_with_denials_still_passes(env: Env) -> None:
    denial = PermissionDenial(tool_name="WebFetch")
    result = await env.run(FakeHarness(_result(denials=(denial,))))
    assert result.outcome == StepOutcome.PASSED
    assert result.summary == "Added the thing."
    assert result.outputs["permission_denials"] == [denial.model_dump()]


async def test_oversized_answer_fails_the_step(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_module, "OUTPUTS_MAX_BYTES", 10)
    result = await env.run(FakeHarness())
    assert result.outcome == StepOutcome.FAILED_ERROR
    assert "byte limit" in result.summary
    assert result.outputs == {}


async def test_prompt_schema_adds_fields_to_the_answer(env: Env) -> None:
    prompts = Path(env.handle.repos[HOME]) / ".fact" / "prompts"
    extra = {"properties": {"title": {"type": "string"}}, "required": ["title"]}
    (prompts / "implement.schema.json").write_text(json.dumps(extra), encoding="utf-8")
    harness = FakeHarness()
    await env.run(harness)
    schema = harness.requests[0].output_schema
    assert schema is not None
    assert set(schema["properties"]) == {"status", "summary", "blocker", "retryable", "title"}
    assert schema["required"] == ["status", "summary", "title"]


async def test_invalid_prompt_schema_is_non_retryable(env: Env) -> None:
    prompts = Path(env.handle.repos[HOME]) / ".fact" / "prompts"
    (prompts / "implement.schema.json").write_text("[1]", encoding="utf-8")
    with pytest.raises(ApplicationError, match="properties") as info:
        await env.run(FakeHarness())
    assert info.value.non_retryable


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
    assert beats[-1] == ("implement: 2 turns", "s-1")  # the session id from the init message


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


async def test_worker_shutdown_fails_as_interrupted_with_the_session(env: Env) -> None:
    activity_env = ActivityEnvironment()
    task = asyncio.create_task(activity_env.run(env.activities(FakeHarness(delay=30)).agent_step, env.input()))
    await asyncio.sleep(0.3)
    activity_env.cancel(ActivityCancellationDetails(worker_shutdown=True))
    with pytest.raises(ApplicationError) as info:
        await task
    assert info.value.type == AGENT_INTERRUPTED
    assert list(info.value.details) == ["s-1"]
    assert info.value.non_retryable
    assert (await env.step())["outcome"] == "failed_error"


async def test_result_carries_the_session_id(env: Env) -> None:
    assert (await env.run(FakeHarness())).session_id == "s-1"


async def test_resume_sends_the_resume_prompt_and_keeps_the_schema(env: Env) -> None:
    prompts = Path(env.handle.repos[HOME]) / ".fact" / "prompts"
    (prompts / "implement.schema.json").write_text('{"properties": {"title": {"type": "string"}}}', encoding="utf-8")
    harness = FakeHarness()
    await env.run(harness, resume_session_id="s-0", context="the fact worker running it stopped.")
    (request,) = harness.requests
    assert request.resume_session_id == "s-0"
    assert request.prompt.startswith("Your previous session on this task stopped before it finished: the fact worker")
    assert request.output_schema is not None
    assert "title" in request.output_schema["properties"]


def test_turns_count_distinct_message_ids() -> None:
    progress = agent_module._Progress()  # noqa: SLF001
    for message_id in ("a", "a", "b", None, None):
        progress.on_message({"type": "assistant", "message": {"id": message_id}})
    progress.on_message({"type": "user"})
    assert progress.turns == 4  # noqa: PLR2004
