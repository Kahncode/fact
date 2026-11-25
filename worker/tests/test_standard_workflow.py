"""`.fact/workflows/standard.py` against fake activities registered under the real names, plus a replay test."""

import importlib
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import pytest
from fact.core.activity_contract import (
    AGENT_INTERRUPTED,
    AGENT_STEP,
    CONTROL_QUEUE,
    MARK_TASK_DONE,
    OPEN_CHANGE_SET,
    PREPARE_WORKSPACE,
    RECORD_RUN_FINISHED,
    RECORD_RUN_STARTED,
    SANDBOX_QUEUE,
    SHELL_STEP,
    TEARDOWN_WORKSPACE,
    AgentStepInput,
    MarkTaskDoneInput,
    OpenChangeSetInput,
    RunFinishedInput,
    RunStartedInput,
    ShellStepInput,
)
from fact.core.enums import RunOutcome, StepOutcome
from fact.core.settings import REPO_ROOT
from fact.core.types import PRRef, RepoCheckout, RunInput, RunResult, StepResult, Task, WorkspaceHandle, WorkspaceSpec
from fact.sdk.sessions import MAX_RESUMES
from fact.worker.loader import sandbox_runner
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError, WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

WORKFLOWS_DIR = REPO_ROOT / ".fact" / "workflows"
WORKFLOW_QUEUE = "wf-test"
IDENTITY = "test-worker"  # kept out of recorded histories instead of the host name
REPO = "acme/widget"
BRANCH = "fact/task-m1-99"
PR = PRRef(repo=REPO, number=7, url=f"https://github.com/{REPO}/pull/7", head_sha="abc123")


def load_standard() -> ModuleType:
    """Import `.fact/workflows/standard.py` by name, so the workflow sandbox can import it again."""
    if str(WORKFLOWS_DIR) not in sys.path:
        sys.path.insert(0, str(WORKFLOWS_DIR))
    return importlib.import_module("standard")


RUN_URL = "http://ui.test/namespaces/default/workflows/run-1"


def run_input(run_id: str = "run-1") -> RunInput:
    task = Task(key="task:M1-99", id="M1-99", title="Add a widget", path="tasks/M1/99-widget.md")
    repo = RepoCheckout(full_name=REPO, default_branch="main", branch=BRANCH)
    return RunInput(run_id=run_id, project="acme", task=task, repos=[repo], run_url=RUN_URL)


@dataclass
class FakeActivities:
    """Scripted step results. `test_failures` is how many `test` checks fail before one passes."""

    test_failures: int = 0
    implement_outcome: StepOutcome = StepOutcome.PASSED
    implement_script: list[StepResult | Exception] = field(default_factory=list)  # first implement sessions, in order
    broken: str | None = None  # name of an activity that raises a non-retryable error
    checked_out: str = BRANCH  # the branch `prepare_workspace` reports, as when another workspace holds `BRANCH`
    calls: list[tuple[str, object]] = field(default_factory=list)

    def _call(self, name: str, spec: object) -> None:
        self.calls.append((name, spec))
        if name == self.broken:
            msg = f"{name} is broken"
            raise ApplicationError(msg, non_retryable=True)

    @activity.defn(name=RECORD_RUN_STARTED)
    async def record_run_started(self, spec: RunStartedInput) -> None:
        self._call(RECORD_RUN_STARTED, spec)

    @activity.defn(name=RECORD_RUN_FINISHED)
    async def record_run_finished(self, spec: RunFinishedInput) -> None:
        self._call(RECORD_RUN_FINISHED, spec)

    @activity.defn(name=PREPARE_WORKSPACE)
    async def prepare_workspace(self, spec: WorkspaceSpec) -> WorkspaceHandle:
        self._call(PREPARE_WORKSPACE, spec)
        root = f"/ws/{spec.run_id}"
        repos = {r.full_name: f"{root}/widget" for r in spec.repos}
        return WorkspaceHandle(
            run_id=spec.run_id, root=root, repos=repos, branches=dict.fromkeys(repos, self.checked_out)
        )

    @activity.defn(name=TEARDOWN_WORKSPACE)
    async def teardown_workspace(self, handle: WorkspaceHandle) -> None:
        self._call(TEARDOWN_WORKSPACE, handle)

    @activity.defn(name=AGENT_STEP)
    async def agent_step(self, spec: AgentStepInput) -> StepResult:
        self._call(AGENT_STEP, spec)
        implementing = spec.prompt_name == "implement"
        if implementing and self.implement_script:
            scripted = self.implement_script.pop(0)
            if isinstance(scripted, Exception):
                raise scripted
            return scripted
        outputs = {"title": "feat(M1-99): add the widget"} if implementing else {}
        outcome = self.implement_outcome if implementing else StepOutcome.PASSED
        return StepResult(outcome=outcome, summary=f"{spec.prompt_name} done", outputs=outputs)

    @activity.defn(name=SHELL_STEP)
    async def shell_step(self, spec: ShellStepInput) -> StepResult:
        self._call(SHELL_STEP, spec)
        if spec.check == "test" and self.test_failures > 0:
            self.test_failures -= 1
            outputs = {"command": ["pytest"], "exit_code": 1, "failure_output": "FAILED test_widget"}
            return StepResult(outcome=StepOutcome.FAILED_CHECKS, summary="`pytest` exited with 1", outputs=outputs)
        return StepResult(outcome=StepOutcome.PASSED)

    @activity.defn(name=MARK_TASK_DONE)
    async def mark_task_done(self, spec: MarkTaskDoneInput) -> bool:
        self._call(MARK_TASK_DONE, spec)
        return True

    @activity.defn(name=OPEN_CHANGE_SET)
    async def open_change_set(self, spec: OpenChangeSetInput) -> list[PRRef]:
        self._call(OPEN_CHANGE_SET, spec)
        return [PR]

    def of(self, name: str) -> list:
        return [arg for n, arg in self.calls if n == name]


@asynccontextmanager
async def workers(client: Client, fakes: FakeActivities, workflow_cls: type) -> AsyncIterator[None]:
    control = [
        fakes.record_run_started,
        fakes.record_run_finished,
        fakes.prepare_workspace,
        fakes.teardown_workspace,
        fakes.mark_task_done,
        fakes.open_change_set,
    ]
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(
            Worker(
                client,
                identity=IDENTITY,
                task_queue=WORKFLOW_QUEUE,
                workflows=[workflow_cls],
                workflow_runner=sandbox_runner(),
            )
        )
        await stack.enter_async_context(Worker(client, identity=IDENTITY, task_queue=CONTROL_QUEUE, activities=control))
        await stack.enter_async_context(
            Worker(client, identity=IDENTITY, task_queue=SANDBOX_QUEUE, activities=[fakes.agent_step, fakes.shell_step])
        )
        yield


_standard = load_standard()
STANDARD = _standard.Standard
MAX_FIX_ATTEMPTS = _standard.MAX_FIX_ATTEMPTS
HISTORY = Path(__file__).parent / "histories" / "standard_green.json"
RECORD_VAR = "FACT_RECORD_HISTORIES"  # set to 1 to re-record the saved histories from the green path


async def _run(fakes: FakeActivities, workflow_id: str) -> tuple[RunResult, WorkflowHistory]:
    env_cm = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter, identity=IDENTITY)
    async with env_cm as env, workers(env.client, fakes, STANDARD):
        handle = await env.client.start_workflow(
            STANDARD.run, run_input(), id=workflow_id, task_queue=WORKFLOW_QUEUE, result_type=RunResult
        )
        result = await handle.result()
        history = await handle.fetch_history()
    return result, history


def _agent_sessions(fakes: FakeActivities) -> list[tuple[str, int, str]]:
    return [(s.stage_id, s.attempt, s.prompt_name) for s in fakes.of(AGENT_STEP)]


def _finished(fakes: FakeActivities) -> RunOutcome:
    (finished,) = fakes.of(RECORD_RUN_FINISHED)
    return finished.outcome


async def test_green_path(tmp_path: Path) -> None:
    fakes = FakeActivities()
    result, history = await _run(fakes, "run-green")

    assert result.outcome == RunOutcome.PR_OPENED
    assert result.prs == [PR]
    assert [n for n, _ in fakes.calls] == [
        RECORD_RUN_STARTED,
        PREPARE_WORKSPACE,
        AGENT_STEP,
        SHELL_STEP,
        SHELL_STEP,
        MARK_TASK_DONE,
        OPEN_CHANGE_SET,
        RECORD_RUN_FINISHED,
        TEARDOWN_WORKSPACE,
    ]
    assert _agent_sessions(fakes) == [("implement", 1, "implement")]
    assert [(s.stage_id, s.attempt, s.check) for s in fakes.of(SHELL_STEP)] == [
        ("checks", 1, "lint"),
        ("checks", 2, "test"),
    ]
    (pr_spec,) = fakes.of(OPEN_CHANGE_SET)
    assert pr_spec.branch == BRANCH
    assert pr_spec.title == "feat(M1-99): add the widget"  # from the implement session's outputs
    assert pr_spec.summary == "implement done"
    assert pr_spec.run_url == RUN_URL
    assert [(c.name, c.outcome) for c in pr_spec.check_results] == [("lint", "passed"), ("test", "passed")]
    assert _finished(fakes) == RunOutcome.PR_OPENED

    if os.environ.get(RECORD_VAR):
        HISTORY.parent.mkdir(exist_ok=True)
        HISTORY.write_text(history.to_json(), encoding="utf-8")
    else:
        (tmp_path / "green.json").write_text(history.to_json(), encoding="utf-8")  # proves the history serializes


async def test_two_failures_then_green() -> None:
    fakes = FakeActivities(test_failures=2)
    result, _ = await _run(fakes, "run-two-failures")

    assert result.outcome == RunOutcome.PR_OPENED
    assert _agent_sessions(fakes) == [
        ("implement", 1, "implement"),
        ("implement", 2, "fix_checks"),
        ("implement", 3, "fix_checks"),
    ]
    fix = fakes.of(AGENT_STEP)[1]
    assert "### test: `pytest` exited with 1" in fix.context
    assert "FAILED test_widget" in fix.context
    assert [s.attempt for s in fakes.of(SHELL_STEP)] == [1, 2, 3, 4, 5, 6]  # three rounds of lint and test
    assert len(fakes.of(OPEN_CHANGE_SET)) == 1


async def test_exhausted_checks_need_a_human() -> None:
    fakes = FakeActivities(test_failures=100)
    result, _ = await _run(fakes, "run-exhausted")

    assert result.outcome == RunOutcome.NEEDS_HUMAN
    assert result.prs == []
    assert "test" in result.summary
    assert len(fakes.of(AGENT_STEP)) == 1 + MAX_FIX_ATTEMPTS
    assert len(fakes.of(SHELL_STEP)) == 2 * (MAX_FIX_ATTEMPTS + 1)
    assert fakes.of(MARK_TASK_DONE) == []
    assert fakes.of(OPEN_CHANGE_SET) == []
    assert _finished(fakes) == RunOutcome.NEEDS_HUMAN
    assert fakes.of(TEARDOWN_WORKSPACE) == []  # left on disk, holding its branch and commits


async def test_pr_goes_on_the_branch_the_workspace_checked_out() -> None:
    fakes = FakeActivities(checked_out=f"{BRANCH}-2")
    result, _ = await _run(fakes, "run-branch-taken")

    assert result.outcome == RunOutcome.PR_OPENED
    (pr_spec,) = fakes.of(OPEN_CHANGE_SET)
    assert pr_spec.branch == f"{BRANCH}-2"


async def test_teardown_failure_does_not_fail_the_run() -> None:
    fakes = FakeActivities(broken=TEARDOWN_WORKSPACE)
    result, _ = await _run(fakes, "run-teardown-broken")
    assert result.outcome == RunOutcome.PR_OPENED


async def test_replay_green_history() -> None:
    assert HISTORY.exists(), f"missing {HISTORY.name}: run this file with {RECORD_VAR}=1 to record it"
    history = WorkflowHistory.from_json("run-green", HISTORY.read_text(encoding="utf-8"))
    replayer = Replayer(workflows=[STANDARD], data_converter=pydantic_data_converter, workflow_runner=sandbox_runner())
    await replayer.replay_workflow(history)


async def test_failed_implement_ends_the_run_without_checks() -> None:
    fakes = FakeActivities(implement_outcome=StepOutcome.FAILED_ERROR)
    result, _ = await _run(fakes, "run-implement-failed")

    assert result.outcome == RunOutcome.FAILED
    assert fakes.of(SHELL_STEP) == []
    assert fakes.of(OPEN_CHANGE_SET) == []
    assert _finished(fakes) == RunOutcome.FAILED


async def test_blocked_implement_needs_a_human_without_a_pr() -> None:
    fakes = FakeActivities(implement_outcome=StepOutcome.NEEDS_HUMAN)
    result, _ = await _run(fakes, "run-implement-blocked")

    assert result.outcome == RunOutcome.NEEDS_HUMAN
    assert "needs_human" in result.summary
    assert fakes.of(SHELL_STEP) == []
    assert fakes.of(MARK_TASK_DONE) == []
    assert fakes.of(OPEN_CHANGE_SET) == []
    assert _finished(fakes) == RunOutcome.NEEDS_HUMAN


RETRYABLE = StepResult(
    outcome=StepOutcome.NEEDS_HUMAN,
    summary="rate limited",
    outputs={"status": "blocked", "retryable": True, "blocker": "API rate limit"},
    session_id="s-1",
)
INTERRUPTED = ApplicationError("worker stopped", "s-1", type=AGENT_INTERRUPTED, non_retryable=True)


async def test_retryable_block_resumes_the_session_once() -> None:
    fakes = FakeActivities(implement_script=[RETRYABLE])
    result, _ = await _run(fakes, "run-retryable")

    assert result.outcome == RunOutcome.PR_OPENED
    first, second = fakes.of(AGENT_STEP)
    assert (first.attempt, first.resume_session_id) == (1, None)
    assert (second.stage_id, second.attempt, second.prompt_name) == ("implement", 2, "implement")
    assert second.resume_session_id == "s-1"
    assert "API rate limit" in second.context


async def test_retryable_block_twice_needs_a_human() -> None:
    fakes = FakeActivities(implement_script=[RETRYABLE, RETRYABLE])
    result, _ = await _run(fakes, "run-retryable-twice")

    assert result.outcome == RunOutcome.NEEDS_HUMAN
    assert len(fakes.of(AGENT_STEP)) == MAX_RESUMES + 1  # one resume, never more
    assert fakes.of(OPEN_CHANGE_SET) == []


async def test_interrupted_worker_resumes_the_session() -> None:
    fakes = FakeActivities(implement_script=[INTERRUPTED])
    result, _ = await _run(fakes, "run-interrupted")

    assert result.outcome == RunOutcome.PR_OPENED
    _, resumed = fakes.of(AGENT_STEP)
    assert (resumed.attempt, resumed.resume_session_id) == (2, "s-1")
    assert "worker" in resumed.context


async def test_interrupted_twice_fails_the_run() -> None:
    fakes = FakeActivities(implement_script=[INTERRUPTED, INTERRUPTED])
    with pytest.raises(WorkflowFailureError):
        await _run(fakes, "run-interrupted-twice")
    assert len(fakes.of(AGENT_STEP)) == MAX_RESUMES + 1
    assert _finished(fakes) == RunOutcome.FAILED
    assert fakes.of(TEARDOWN_WORKSPACE) == []


async def test_run_bookkeeping_failure_does_not_fail_the_run() -> None:
    fakes = FakeActivities(broken=RECORD_RUN_STARTED)
    result, _ = await _run(fakes, "run-api-down")
    assert result.outcome == RunOutcome.PR_OPENED


async def test_failing_activity_fails_the_run_and_records_it() -> None:
    fakes = FakeActivities(broken=PREPARE_WORKSPACE)
    with pytest.raises(WorkflowFailureError):
        await _run(fakes, "run-prepare-broken")
    assert _finished(fakes) == RunOutcome.FAILED


def test_standard_stage_manifest() -> None:
    assert STANDARD.manifest() == [
        {"id": "prepare", "label": "Prepare workspace", "kind": "shell", "loops_to": None},
        {"id": "implement", "label": "Implement", "kind": "agent", "loops_to": None},
        {"id": "checks", "label": "Lint and test", "kind": "shell", "loops_to": "implement"},
        {"id": "open_pr", "label": "Open PR", "kind": "github", "loops_to": None},
    ]
    assert json.loads(json.dumps(STANDARD.manifest()))[2]["loops_to"] == "implement"
