"""
`python -m fact.worker`: run the workers until interrupted.

One workflow worker per registered project on `wf-<project>` (versioned and pinned, see `deployments.py`), plus the
`control` and `sandbox` activity workers; the sandbox worker runs at most `concurrency.sandboxes` activities at once.
Startup deletes a leftover `fact-scheduler` Schedule (`scheduler.py`): the scheduler ticks in the API now. While runs
are active the worker keeps Windows awake (`keep_awake.py`).
"""

import asyncio
import signal
import sys
from contextlib import AsyncExitStack

import httpx
from fact.core.github import GitHubApp, GitHubAppConfigError, shared_app
from fact.core.logging import configure_logging, get_logger
from fact.core.settings import Settings, get_settings
from fact.worker.activities.agent import AgentActivities
from fact.worker.activities.change_set import ChangeSetActivities
from fact.worker.activities.ci import CIActivities
from fact.worker.activities.classify import ClassifyActivities
from fact.worker.activities.control import ControlActivities
from fact.worker.activities.cost_comment import CostCommentActivities
from fact.worker.activities.gates import GateActivities
from fact.worker.activities.handoff import HandoffActivities
from fact.worker.activities.merge import MergeActivities
from fact.worker.activities.notify import NotifyActivities
from fact.worker.activities.pr_state import PRStateActivities
from fact.worker.activities.shell import ShellActivities
from fact.worker.activities.task_status import TaskStatusActivities
from fact.worker.activities.workspace import WorkspaceActivities
from fact.worker.client import connect
from fact.worker.deployments import deployment_config, set_current_version
from fact.worker.harnesses import ClaudeCodeHarness
from fact.worker.keep_awake import KeepAwake, api_active_runs, watch_active_runs
from fact.worker.loader import ProjectWorkflows, WorkflowLoadError, factory_sha, load_project, sandbox_runner
from fact.worker.ping import CONTROL_QUEUE, SANDBOX_QUEUE, PingWorkflow, ping
from fact.worker.reporting import StepReporter
from fact.worker.runners import LocalWorktreeRunner, RunnerError
from fact.worker.scheduler import delete_leftover_schedule
from temporalio.client import Client
from temporalio.worker import Worker

HTTP_TIMEOUT_S = 30.0


def _cancel_on_ctrl_break() -> None:
    """On Windows, `fact dev` stops children with Ctrl-Break; drain the workers as for Ctrl-C."""
    if sys.platform != "win32":
        return
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    if task is not None:
        signal.signal(signal.SIGBREAK, lambda *_: loop.call_soon_threadsafe(task.cancel))


def _github_app(settings: Settings) -> GitHubApp | None:
    """Build the process's one GitHub App, or None when it is not configured (pushing and PRs then fail)."""
    try:
        return shared_app(settings)
    except GitHubAppConfigError as exc:
        get_logger("fact.worker").warning("github_app_unavailable", error=str(exc))
        return None


async def load_projects(settings: Settings, runner: LocalWorktreeRunner) -> list[ProjectWorkflows]:
    """Load every registered project's workflows; a project that fails to load is logged and skipped."""
    log = get_logger("fact.worker")
    factory = await factory_sha()
    loaded: list[ProjectWorkflows] = []
    for ref in settings.projects:
        try:
            project = await load_project(ref.name, ref.home, runner.sync_mirror, settings.pinned_dir, factory)
        except (WorkflowLoadError, RunnerError) as exc:
            log.error("workflow_load_failed", project=ref.name, error=str(exc))  # noqa: TRY400 - the message is enough
            continue
        if not project.workflows:
            log.warning("no_workflows", project=ref.name, checkout=str(project.checkout))
            continue
        loaded.append(project)
    return loaded


def workflow_worker(client: Client, project: ProjectWorkflows) -> Worker:
    return Worker(
        client,
        task_queue=project.task_queue,
        workflows=list(project.workflows),
        workflow_runner=sandbox_runner(),
        deployment_config=deployment_config(project),
    )


def _activity_workers(
    client: Client, settings: Settings, http: httpx.AsyncClient, runner: LocalWorktreeRunner, github: GitHubApp | None
) -> list[Worker]:
    reporter = StepReporter(settings.api_url, http, log_dir=settings.data_dir / "step-logs")
    workspace = WorkspaceActivities(runner, reporter)
    shell = ShellActivities(runner, reporter)
    harness = ClaudeCodeHarness()
    agent = AgentActivities({harness.name: harness}, reporter, settings)
    change_set = ChangeSetActivities(github, reporter)
    control_steps = ControlActivities(settings.api_url, http, settings)
    task_status = TaskStatusActivities(settings)
    classify = ClassifyActivities(settings)
    gates = GateActivities(settings.api_url, http)
    notify = NotifyActivities(settings.api_url, http)
    handoff = HandoffActivities(settings.api_url, http)
    cost = CostCommentActivities(github, settings.api_url, http)
    ci = CIActivities(github, reporter)
    pr_state = PRStateActivities(github)
    merge = MergeActivities(github)
    control = Worker(
        client,
        task_queue=CONTROL_QUEUE,
        workflows=[PingWorkflow],
        activities=[
            ping,
            workspace.prepare_workspace,
            workspace.teardown_workspace,
            workspace.sync_base,
            change_set.open_change_set,
            change_set.label_change_set,
            cost.post_cost_comment,
            ci.wait_for_ci,
            pr_state.wait_for_pr_closed,
            merge.merge_change_set,
            control_steps.mark_task_done,
            control_steps.record_run_started,
            control_steps.record_run_waiting,
            control_steps.record_run_finished,
            task_status.report_status,
            classify.classify_signals,
            gates.record_gate_opened,
            gates.record_gate_answered,
            notify.notify,
            handoff.write_handoff,
        ],
    )
    sandbox = Worker(
        client,
        task_queue=SANDBOX_QUEUE,
        activities=[shell.shell_step, agent.agent_step],
        max_concurrent_activities=settings.concurrency.sandboxes,
    )
    return [control, sandbox]


async def run() -> None:
    _cancel_on_ctrl_break()
    settings = get_settings()
    configure_logging(settings.env)
    client = await connect(settings)
    log = get_logger("fact.worker")
    github = _github_app(settings)
    runner = LocalWorktreeRunner(settings, github=github)
    projects = await load_projects(settings, runner)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as http, AsyncExitStack() as stack:
        activity_workers = _activity_workers(client, settings, http, runner, github)
        workers = [*activity_workers, *(workflow_worker(client, p) for p in projects)]
        for worker in workers:
            await stack.enter_async_context(worker)
        for project in projects:
            await set_current_version(client, project)  # failing here beats runs that never start
        await delete_leftover_schedule(client)
        queues = [CONTROL_QUEUE, SANDBOX_QUEUE, *(p.task_queue for p in projects)]
        log.info("worker_started", queues=queues, address=settings.temporal.address)
        # on this (the event-loop) thread: the keep-awake hold lasts only while its calling thread lives
        keeper = KeepAwake(enabled=settings.worker.keep_awake)
        await watch_active_runs(keeper, api_active_runs(settings.api_url, http))  # until cancelled


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt, asyncio.CancelledError:
        get_logger("fact.worker").info("worker_stopped")


if __name__ == "__main__":
    main()
