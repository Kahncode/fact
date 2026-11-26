"""Harness for the `Standard` workflow tests: fake activities registered under the real names, and a test server."""

import importlib
import sys
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from types import ModuleType

from fact.core.activity_contract import (
    AGENT_STEP,
    CONTROL_QUEUE,
    MARK_TASK_DONE,
    OPEN_CHANGE_SET,
    POST_COST_COMMENT,
    PREPARE_WORKSPACE,
    RECORD_GATE_ANSWERED,
    RECORD_GATE_OPENED,
    RECORD_RUN_FINISHED,
    RECORD_RUN_STARTED,
    SANDBOX_QUEUE,
    SHELL_STEP,
    TEARDOWN_WORKSPACE,
    WAIT_FOR_CI,
    AgentStepInput,
    GateAnsweredInput,
    GateOpenedInput,
    MarkTaskDoneInput,
    OpenChangeSetInput,
    PostCostCommentInput,
    RunFinishedInput,
    RunStartedInput,
    ShellStepInput,
    WaitForCIInput,
)
from fact.core.enums import RunOutcome, StepOutcome
from fact.core.gates import GateDecision
from fact.core.settings import REPO_ROOT
from fact.core.types import PRRef, RepoCheckout, RunInput, RunResult, StepResult, Task, WorkspaceHandle, WorkspaceSpec
from fact.worker.loader import sandbox_runner
from gate_harness import answer
from temporalio import activity
from temporalio.client import Client, WorkflowHandle, WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

WORKFLOWS_DIR = REPO_ROOT / ".fact" / "workflows"
WORKFLOW_QUEUE = "wf-test"
IDENTITY = "test-worker"  # a fixed worker identity instead of the host name
REPO = "acme/widget"
BRANCH = "fact/task-m1-99"
PR = PRRef(repo=REPO, number=7, url=f"https://github.com/{REPO}/pull/7", head_sha="abc123")


def load_standard() -> ModuleType:
    """Import `.fact/workflows/standard.py` by name, so the workflow sandbox can import it again."""
    if str(WORKFLOWS_DIR) not in sys.path:
        sys.path.insert(0, str(WORKFLOWS_DIR))
    return importlib.import_module("standard")


RUN_URL = "http://ui.test/namespaces/default/workflows/run-1"
MET = {"text": "The widget renders", "met": True, "evidence": "test_widget passes"}
UNMET = {"text": "The widget is documented", "met": False, "evidence": "README not updated"}
LOW = {"severity": "low", "file": "widget.py", "line": 3, "message": "name could be clearer"}
HIGH = {"severity": "high", "file": "widget.py", "line": 9, "message": "crashes on empty input"}
CLEAN_REVIEW = {"status": "done", "summary": "looks good", "criteria": [MET], "findings": [LOW]}
LEARNED = {"status": "done", "title": "feat(M1-99): widget, documented", "summary": "Adds the widget and its docs."}


def run_input(run_id: str = "run-1") -> RunInput:
    task = Task(key="task:M1-99", id="M1-99", title="Add a widget", path="tasks/M1/99-widget.md")
    repo = RepoCheckout(full_name=REPO, default_branch="main", branch=BRANCH)
    return RunInput(run_id=run_id, project="acme", task=task, repos=[repo], run_url=RUN_URL)


@dataclass
class FakeActivities:
    """Scripted step results. `test_failures` is how many `test` checks fail before one passes."""

    test_failures: int = 0
    implement_outcome: StepOutcome = StepOutcome.PASSED
    setup_outcome: StepOutcome = StepOutcome.PASSED
    implement_script: list[StepResult | Exception] = field(default_factory=list)  # first implement sessions, in order
    review: StepResult = field(
        default_factory=lambda: StepResult(outcome=StepOutcome.PASSED, summary="looks good", outputs=CLEAN_REVIEW)
    )
    address_outcome: StepOutcome = StepOutcome.PASSED
    learn: StepResult | Exception = field(
        default_factory=lambda: StepResult(outcome=StepOutcome.PASSED, outputs=LEARNED)
    )
    test_failures_after_address: int = 0  # `test` failures once `address_review` has run
    broken: str | None = None  # name of an activity that raises a non-retryable error
    checked_out: str = BRANCH  # the branch `prepare_workspace` reports, as when another workspace holds `BRANCH`
    ci_failures: int = 0  # `wait_for_ci` steps that fail before one passes
    ci_timed_out: bool = False  # `wait_for_ci` gives up on a CI that never finishes
    fix_ci_outcome: StepOutcome = StepOutcome.PASSED
    fix_ci_commits: bool = True  # False: a `fix_ci` session that passes but commits nothing, so the head stays
    # How `record_gate_opened` answers each gate, in order, then reject; None: the test answers them itself.
    # Answering inside the activity keeps the time-skipping server busy, so it never skips to the workflow's timeout.
    gate_decisions: list[GateDecision] | None = field(default_factory=list)
    client: Client | None = None  # set by `started_standard`, to answer gates
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

    @activity.defn(name=RECORD_GATE_OPENED)
    async def record_gate_opened(self, spec: GateOpenedInput) -> str:
        self._call(RECORD_GATE_OPENED, spec)
        if self.gate_decisions is not None and self.client is not None:
            decision = self.gate_decisions.pop(0) if self.gate_decisions else GateDecision.REJECT
            handle = self.client.get_workflow_handle(activity.info().workflow_id)
            await answer(handle, spec.gate_id, decision)
        return f"step-{spec.gate_id}"

    @activity.defn(name=RECORD_GATE_ANSWERED)
    async def record_gate_answered(self, spec: GateAnsweredInput) -> None:
        self._call(RECORD_GATE_ANSWERED, spec)

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
        if spec.prompt_name == "review":
            return self.review
        if spec.prompt_name == "learn":
            if isinstance(self.learn, Exception):
                raise self.learn
            return self.learn
        if spec.prompt_name == "fix_ci":
            return StepResult(outcome=self.fix_ci_outcome, summary="fixed CI")
        if spec.prompt_name == "address_review":
            self.test_failures += self.test_failures_after_address
            return StepResult(outcome=self.address_outcome, summary="addressed", outputs={"summary": "addressed"})
        outputs = {"title": "feat(M1-99): add the widget"} if implementing else {}
        outcome = self.implement_outcome if implementing else StepOutcome.PASSED
        return StepResult(outcome=outcome, summary=f"{spec.prompt_name} done", outputs=outputs)

    @activity.defn(name=SHELL_STEP)
    async def shell_step(self, spec: ShellStepInput) -> StepResult:
        self._call(SHELL_STEP, spec)
        if spec.check == "setup":
            return StepResult(outcome=self.setup_outcome, summary="`uv sync` exited with 1")
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
        pushes = len(self.of(OPEN_CHANGE_SET))
        fixed = pushes - 1 if self.fix_ci_commits else 0
        return [PR if fixed == 0 else PR.model_copy(update={"head_sha": f"fix{fixed}"})]

    @activity.defn(name=POST_COST_COMMENT)
    async def post_cost_comment(self, spec: PostCostCommentInput) -> None:
        self._call(POST_COST_COMMENT, spec)

    @activity.defn(name=WAIT_FOR_CI)
    async def wait_for_ci(self, spec: WaitForCIInput) -> StepResult:
        self._call(WAIT_FOR_CI, spec)
        if self.ci_timed_out:
            return StepResult(
                outcome=StepOutcome.FAILED_CHECKS, summary="CI still running", outputs={"timed_out": True}
            )
        if self.ci_failures > 0:
            self.ci_failures -= 1
            failure = {"name": "test (windows)", "conclusion": "failure", "url": "https://ci/1", "log_tail": "E boom"}
            return StepResult(outcome=StepOutcome.FAILED_CHECKS, summary="1 failed", outputs={"failures": [failure]})
        return StepResult(outcome=StepOutcome.PASSED, summary="CI passed")

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
        fakes.post_cost_comment,
        fakes.wait_for_ci,
        fakes.record_gate_opened,
        fakes.record_gate_answered,
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


@asynccontextmanager
async def started_standard(
    fakes: FakeActivities, workflow_id: str
) -> AsyncIterator[tuple[WorkflowEnvironment, WorkflowHandle]]:
    """Start `Standard` on a time-skipping server; the test drives it (gates, signals) and awaits the result."""
    env_cm = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter, identity=IDENTITY)
    async with env_cm as env, workers(env.client, fakes, STANDARD):
        fakes.client = env.client
        handle = await env.client.start_workflow(
            STANDARD.run, run_input(), id=workflow_id, task_queue=WORKFLOW_QUEUE, result_type=RunResult
        )
        yield env, handle


async def run_standard(fakes: FakeActivities, workflow_id: str) -> tuple[RunResult, WorkflowHistory]:
    """Run `Standard` to its end; `fakes.gate_decisions` answers its gates."""
    async with started_standard(fakes, workflow_id) as (_, handle):
        result = await handle.result()
        history = await handle.fetch_history()
    return result, history


def agent_sessions(fakes: FakeActivities) -> list[tuple[str, int, str]]:
    return [(s.stage_id, s.attempt, s.prompt_name) for s in fakes.of(AGENT_STEP)]


def finished(fakes: FakeActivities) -> RunOutcome:
    (finished,) = fakes.of(RECORD_RUN_FINISHED)
    return finished.outcome


def checks(fakes: FakeActivities) -> list[ShellStepInput]:
    return [s for s in fakes.of(SHELL_STEP) if s.stage_id == "checks"]
