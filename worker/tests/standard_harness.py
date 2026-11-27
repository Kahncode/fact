"""Harness for the `Standard` workflow tests: fake activities registered under the real names, and a test server."""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field

from fact.core.activity_contract import (
    AGENT_STEP,
    CONTROL_QUEUE,
    MARK_TASK_DONE,
    NOTIFY,
    PREPARE_WORKSPACE,
    RECORD_GATE_ANSWERED,
    RECORD_GATE_OPENED,
    RECORD_RUN_FINISHED,
    RECORD_RUN_STARTED,
    RECORD_RUN_WAITING,
    REPORT_STATUS,
    SANDBOX_QUEUE,
    SHELL_STEP,
    TEARDOWN_WORKSPACE,
    WRITE_HANDOFF,
    AgentStepInput,
    GateAnsweredInput,
    GateOpenedInput,
    HandoffInput,
    MarkTaskDoneInput,
    NotifyInput,
    ReportStatusInput,
    RunFinishedInput,
    RunStartedInput,
    RunWaitingInput,
    ShellStepInput,
)
from fact.core.enums import StepOutcome
from fact.core.gates import GateDecision
from fact.core.types import RunInput, RunResult, StepResult, WorkspaceHandle, WorkspaceSpec
from fact.sdk.pull_requests import REVIEW_STAGE
from fact.worker.loader import sandbox_runner
from gate_harness import answer
from standard_env import OPTIONS, WORKFLOW_QUEUE, load_standard
from standard_inputs import BRANCH, CLEAN_REVIEW, LEARNED, LOW_RISK, run_input
from standard_pr_fakes import FakePRSteps
from standard_results import agent_sessions, checks, finished  # noqa: F401 - re-exported for the tests
from temporal_env import terminate_if_running, time_skipping_env
from temporalio import activity
from temporalio.client import Client, WorkflowHandle, WorkflowHistory
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker


@dataclass
class FakeActivities(FakePRSteps):
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
    classify: StepResult = field(default_factory=lambda: StepResult(outcome=StepOutcome.PASSED, outputs=LOW_RISK))
    test_failures_after_address: int = 0  # `test` failures once `address_review` has run
    checked_out: str = BRANCH  # the branch `prepare_workspace` reports, as when another workspace holds `BRANCH`
    fix_ci_outcome: StepOutcome = StepOutcome.PASSED
    resolve_outcome: StepOutcome = StepOutcome.PASSED  # how each `resolve_conflicts` session ends
    test_failures_after_resolve: int = 0  # `test` failures once a `resolve_conflicts` session has run
    # How `record_gate_opened` answers each gate, in order, then reject; None: the test answers them itself.
    # Answering inside the activity keeps the time-skipping server busy, so it never skips to the workflow's timeout.
    gate_decisions: list[GateDecision] | None = field(default_factory=list)
    client: Client | None = None  # set by `started_standard`, to answer gates

    @activity.defn(name=RECORD_RUN_STARTED)
    async def record_run_started(self, spec: RunStartedInput) -> None:
        self._call(RECORD_RUN_STARTED, spec)

    @activity.defn(name=RECORD_RUN_FINISHED)
    async def record_run_finished(self, spec: RunFinishedInput) -> None:
        self._call(RECORD_RUN_FINISHED, spec)

    @activity.defn(name=RECORD_RUN_WAITING)
    async def record_run_waiting(self, spec: RunWaitingInput) -> None:
        self._call(RECORD_RUN_WAITING, spec)

    @activity.defn(name=RECORD_GATE_OPENED)
    async def record_gate_opened(self, spec: GateOpenedInput) -> str:
        self._call(RECORD_GATE_OPENED, spec)
        if spec.kind == REVIEW_STAGE:  # GitHub answers it, through `wait_for_pr_closed`
            return f"step-{spec.gate_id}"
        if self.gate_decisions is not None and self.client is not None:
            decision = self.gate_decisions.pop(0) if self.gate_decisions else GateDecision.REJECT
            handle = self.client.get_workflow_handle(activity.info().workflow_id)
            await answer(handle, spec.gate_id, decision)
        return f"step-{spec.gate_id}"

    @activity.defn(name=RECORD_GATE_ANSWERED)
    async def record_gate_answered(self, spec: GateAnsweredInput) -> None:
        self._call(RECORD_GATE_ANSWERED, spec)

    @activity.defn(name=NOTIFY)
    async def notify(self, spec: NotifyInput) -> int:
        self._call(NOTIFY, spec)
        return len(self.of(NOTIFY))

    @activity.defn(name=REPORT_STATUS)
    async def report_status(self, spec: ReportStatusInput) -> None:
        self._call(REPORT_STATUS, spec)

    @activity.defn(name=WRITE_HANDOFF)
    async def write_handoff(self, spec: HandoffInput) -> str:
        self._call(WRITE_HANDOFF, spec)
        return f"runs/{spec.run_id}/handoff.md"

    @activity.defn(name=PREPARE_WORKSPACE)
    async def prepare_workspace(self, spec: WorkspaceSpec) -> WorkspaceHandle:
        self._call(PREPARE_WORKSPACE, spec)
        root = f"/ws/{spec.run_id}"
        repos = {r.full_name: f"{root}/widget" for r in spec.repos}
        branches = dict.fromkeys(repos, self.checked_out)
        return WorkspaceHandle(run_id=spec.run_id, root=root, repos=repos, branches=branches)

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
        if spec.prompt_name in {"review", "classify"}:
            return self.review if spec.prompt_name == "review" else self.classify
        if spec.prompt_name == "learn":
            if isinstance(self.learn, Exception):
                raise self.learn
            return self.learn
        fixes = {"fix_ci": (self.fix_ci_outcome, "fixed CI"), "resolve_conflicts": (self.resolve_outcome, "resolved")}
        if spec.prompt_name == "resolve_conflicts":
            self.test_failures += self.test_failures_after_resolve
        if spec.prompt_name in fixes:
            outcome, summary = fixes[spec.prompt_name]
            return StepResult(outcome=outcome, summary=summary)
        if spec.prompt_name == "address_review":
            self.test_failures += self.test_failures_after_address
            return StepResult(outcome=self.address_outcome, summary="addressed", outputs={"summary": "addressed"})
        outputs = {"title": "feat(widget): add the widget"} if implementing else {}
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

    def gates(self, name: str) -> list:
        """Return the calls of a gate activity for gates a person answers, without the review wait."""
        return [g for g in self.of(name) if g.kind != REVIEW_STAGE]


@asynccontextmanager
async def workers(client: Client, fakes: FakeActivities, workflow_cls: type) -> AsyncIterator[None]:
    control = [
        fakes.record_run_started,
        fakes.record_run_finished,
        fakes.record_run_waiting,
        fakes.prepare_workspace,
        fakes.teardown_workspace,
        fakes.mark_task_done,
        fakes.open_change_set,
        fakes.post_cost_comment,
        fakes.wait_for_ci,
        fakes.wait_for_pr_closed,
        fakes.merge_change_set,
        fakes.classify_signals,
        fakes.label_change_set,
        fakes.sync_base,
        fakes.record_gate_opened,
        fakes.record_gate_answered,
        fakes.notify,
        fakes.write_handoff,
        fakes.report_status,
    ]
    async with AsyncExitStack() as stack:
        workflows = Worker(
            client, task_queue=WORKFLOW_QUEUE, workflows=[workflow_cls], workflow_runner=sandbox_runner(), **OPTIONS
        )
        await stack.enter_async_context(workflows)
        await stack.enter_async_context(Worker(client, task_queue=CONTROL_QUEUE, activities=control, **OPTIONS))
        sandbox = [fakes.agent_step, fakes.shell_step]
        await stack.enter_async_context(Worker(client, task_queue=SANDBOX_QUEUE, activities=sandbox, **OPTIONS))
        yield


_standard = load_standard()
STANDARD = _standard.Standard
MAX_FIX_ATTEMPTS = _standard.MAX_FIX_ATTEMPTS
MAX_BASE_SYNCS = _standard.MAX_BASE_SYNCS


@asynccontextmanager
async def started_standard(
    fakes: FakeActivities, workflow_id: str, run: RunInput | None = None
) -> AsyncIterator[tuple[WorkflowEnvironment, WorkflowHandle]]:
    """Start `Standard` on a time-skipping server; the test drives it (gates, signals) and awaits the result."""
    env = await time_skipping_env()
    async with workers(env.client, fakes, STANDARD):
        fakes.client = env.client
        handle = await env.client.start_workflow(
            STANDARD.run, run or run_input(), id=workflow_id, task_queue=WORKFLOW_QUEUE, result_type=RunResult
        )
        try:
            yield env, handle
        finally:
            await terminate_if_running(handle)


async def run_standard(
    fakes: FakeActivities, workflow_id: str, run: RunInput | None = None
) -> tuple[RunResult, WorkflowHistory]:
    """Run `Standard` to its end (on `run`, else `run_input()`); `fakes.gate_decisions` answers its gates."""
    async with started_standard(fakes, workflow_id, run) as (_, handle):
        result = await handle.result()
        history = await handle.fetch_history()
    return result, history
