"""The handoff note: rendered, stored by `write_handoff` through the API, and read back into `$handoff` by `agent_step`."""

from pathlib import Path

import pytest
from fact.core.activity_contract import HandoffInput, HandoffStep
from fact.core.artifacts import handoff_key
from fact.core.enums import StepOutcome
from fact.core.settings import RoutingSettings, Settings
from fact.core.types import PRRef, Route, Task, WorkspaceHandle
from fact.worker.activities.agent import AgentActivities, AgentStepInput
from fact.worker.activities.handoff import HandoffActivities
from fact.worker.handoff import HANDOFF_HEADING, render_handoff
from fact.worker.prompts import render, variables
from fact.worker.reporting import StepReporter
from httpx import AsyncClient
from temporalio.testing import ActivityEnvironment
from test_agent_step import FakeHarness

TASK = Task(key="task:M2-99", id="M2-99", title="Drop me", body="Do it")
PR = PRRef(repo="acme/widget", number=7, url="https://github.com/acme/widget/pull/7", head_sha="abc")
REPO = "acme/widget"


def _note(run_id: str = "run-0", **extra: object) -> HandoffInput:
    values: dict[str, object] = {
        "run_id": run_id,
        "task": TASK,
        "reason": "newer prompts",
        "stage_id": "wait_for_review",
        "attempt": 1,
        "steps": [
            HandoffStep(stage_id="implement", attempt=1, outcome=StepOutcome.PASSED, summary="Built the widget."),
            HandoffStep(stage_id="review", attempt=1, outcome=StepOutcome.PASSED, summary="Two findings."),
        ],
        "prs": [PR],
    }
    return HandoffInput.model_validate(values | extra)


def test_note_holds_stage_summaries_and_prs() -> None:
    text = render_handoff(_note())
    assert text.startswith(HANDOFF_HEADING)
    assert "- Reason: newer prompts" in text
    assert "- Last stage reached: `wait_for_review` (attempt 1)" in text
    assert PR.url in text
    assert "### implement (attempt 1): passed\n\nBuilt the widget." in text
    assert "### review (attempt 1): passed\n\nTwo findings." in text


def test_note_of_a_run_dropped_before_any_step() -> None:
    text = render_handoff(_note(reason="", stage_id=None, attempt=None, steps=[], prs=[]))
    assert "- Reason: none given" in text
    assert "had not started a step" in text
    assert "It ran no agent session." in text
    assert "Open PRs" not in text


def test_handoff_placeholder_is_known_and_empty_by_default() -> None:
    assert variables(TASK, "", "/ws")["handoff"] == ""
    assert render("A$handoff.", variables(TASK, "", "/ws", handoff="B")) == "AB."


@pytest.fixture
def workspace(tmp_path: Path, run_id: str) -> WorkspaceHandle:
    prompts = tmp_path / "ws" / "widget" / ".fact" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "implement.md").write_text("Implement $task_id.\n\n$handoff", encoding="utf-8")
    return WorkspaceHandle(run_id=run_id, root=str(tmp_path / "ws"), repos={REPO: str(prompts.parents[1])})


def _agent(client: AsyncClient, tmp_path: Path, settings: Settings, harness: FakeHarness) -> AgentActivities:
    reporter = StepReporter("http://test", client, log_dir=tmp_path / "logs")
    return AgentActivities({harness.name: harness}, reporter, settings)


def _input(workspace: WorkspaceHandle, handoff_ref: str | None) -> AgentStepInput:
    return AgentStepInput(
        run_id=workspace.run_id,
        stage_id="implement",
        attempt=1,
        handle=workspace,
        task=TASK,
        prompt_name="implement",
        handoff_ref=handoff_ref,
    )


@pytest.fixture
def fake_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"routing": RoutingSettings(default=Route(harness="fake", model="sonnet"))})


async def test_the_note_reaches_the_implement_prompt(
    client: AsyncClient, tmp_path: Path, fake_settings: Settings, workspace: WorkspaceHandle
) -> None:
    key = await ActivityEnvironment().run(HandoffActivities("http://test", client).write_handoff, _note())
    assert key == handoff_key("run-0")
    harness = FakeHarness()
    await ActivityEnvironment().run(_agent(client, tmp_path, fake_settings, harness).agent_step, _input(workspace, key))
    (request,) = harness.requests
    assert request.prompt.startswith("Implement M2-99.\n\n" + HANDOFF_HEADING)
    assert "Built the widget." in request.prompt


async def test_no_note_or_a_missing_one_leaves_handoff_empty(
    client: AsyncClient, tmp_path: Path, fake_settings: Settings, workspace: WorkspaceHandle
) -> None:
    harness = FakeHarness()
    agent = _agent(client, tmp_path, fake_settings, harness)
    await ActivityEnvironment().run(agent.agent_step, _input(workspace, None))
    await ActivityEnvironment().run(
        agent.agent_step, _input(workspace, handoff_key("gone")).model_copy(update={"attempt": 2})
    )
    assert [r.prompt for r in harness.requests] == ["Implement M2-99.\n\n", "Implement M2-99.\n\n"]
    log = (tmp_path / "logs" / workspace.run_id / "implement" / "2" / "output.log").read_text(encoding="utf-8")
    assert "handoff note runs/gone/handoff.md: unavailable, left out" in log
