"""
`python -m fact.worker`: run the workers until interrupted.

One workflow worker per registered project on `wf-<project>` (versioned and pinned, see `deployments.py`), and for
each the `control` and workspace activity workers of the same version (`activity_workers.py`): every activity on the
queue of its spec's zone, the workspace ones on the runner's queue (`sandbox`), at most `concurrency.sandboxes` at once
across every project version (one shared `PoolSlots`). Every activity heartbeats through `heartbeats.HeartbeatInterceptor`.
Startup deletes a leftover `fact-scheduler` Schedule (`scheduler.py`): the scheduler ticks in the API now. While runs
are active the worker keeps Windows awake (`keep_awake.py`).
"""

import asyncio
import signal
import sys
from contextlib import AsyncExitStack

import httpx
from fact.core.activity_contract import CONTROL_QUEUE
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
from fact.worker.activity_workers import Activity, PoolSlots, activity_workers
from fact.worker.client import connect
from fact.worker.deployments import deployment_config, set_current_version
from fact.worker.harnesses import ClaudeCodeHarness
from fact.worker.keep_awake import KeepAwake, api_active_runs, watch_active_runs
from fact.worker.loader import ProjectWorkflows, WorkflowLoadError, factory_sha, load_project, sandbox_runner
from fact.worker.reporting import StepReporter
from fact.worker.runners import LocalRunner, RunnerError
from fact.worker.scheduler import delete_leftover_schedule
from temporalio.client import Client
from temporalio.worker import Worker

HTTP_TIMEOUT_S = 30.0


class DuplicateVersionError(RuntimeError):
    """Two registered projects share a build id: the process polls for one, so the other's runs would stall."""


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


async def load_projects(settings: Settings, runner: LocalRunner) -> list[ProjectWorkflows]:
    """
    Load every registered project's workflows; a project that fails to load is logged and skipped.

    Raise `DuplicateVersionError` when two projects share a build id (one home repository at one SHA).
    """
    log = get_logger("fact.worker")
    factory = await factory_sha()
    loaded: list[ProjectWorkflows] = []
    for ref in settings.projects:
        try:
            project = await load_project(ref.name, ref.home, runner.remote_url, settings.pinned_dir, factory)
        except (WorkflowLoadError, RunnerError) as exc:
            log.error("workflow_load_failed", project=ref.name, error=str(exc))  # noqa: TRY400 - the message is enough
            continue
        if not project.workflows:
            log.warning("no_workflows", project=ref.name, checkout=str(project.checkout))
            continue
        clash = next((p for p in loaded if p.version == project.version), None)
        if clash is not None:  # one process polls a queue once per build id
            msg = f"projects {clash.project!r} and {ref.name!r} share build id {project.version}: register their home once"
            raise DuplicateVersionError(msg)
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


def factory_activities(
    settings: Settings, http: httpx.AsyncClient, runner: LocalRunner, github: GitHubApp | None
) -> list[Activity]:
    """Every activity the worker registers; `activity_workers` puts each on the queue of its spec's zone."""
    reporter = StepReporter(settings.api_url, http, log_dir=settings.data_dir / "step-logs")
    workspace = WorkspaceActivities(runner, reporter)
    harness = ClaudeCodeHarness()
    agent = AgentActivities({harness.name: harness}, reporter)
    change_set = ChangeSetActivities(github, reporter)
    control = ControlActivities(settings.api_url, http, settings)
    gates = GateActivities(settings.api_url, http)
    return [
        workspace.prepare_workspace,
        workspace.teardown_workspace,
        workspace.sync_base,
        ShellActivities(runner, reporter).shell_step,
        agent.agent_step,
        change_set.open_change_set,
        change_set.label_change_set,
        CostCommentActivities(github, settings.api_url, http).post_cost_comment,
        CIActivities(github, reporter).wait_for_ci,
        PRStateActivities(github).wait_for_pr_closed,
        MergeActivities(github).merge_change_set,
        control.mark_task_done,
        control.record_run_started,
        control.record_run_waiting,
        control.record_run_finished,
        TaskStatusActivities(settings).report_status,
        ClassifyActivities(settings).classify_signals,
        gates.record_gate_opened,
        gates.record_gate_answered,
        NotifyActivities(settings.api_url, http).notify,
        HandoffActivities(settings.api_url, http).write_handoff,
    ]


def _project_workers(
    client: Client,
    project: ProjectWorkflows,
    activities: list[Activity],
    runner: LocalRunner,
    sandboxes: PoolSlots,
) -> list[Worker]:
    workers = activity_workers(
        client, project, activities, workspace_queue=runner.task_queue, workspace_slots=sandboxes
    )
    return [*workers, workflow_worker(client, project)]


async def run() -> None:
    _cancel_on_ctrl_break()
    settings = get_settings()
    configure_logging(settings.env)
    client = await connect(settings)
    log = get_logger("fact.worker")
    github = _github_app(settings)
    runner = LocalRunner(settings, github=github)
    projects = await load_projects(settings, runner)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as http, AsyncExitStack() as stack:
        activities = factory_activities(settings, http, runner, github)
        sandboxes = PoolSlots(settings.concurrency.sandboxes)  # the runner pool's cap, shared by every version
        workers = [w for p in projects for w in _project_workers(client, p, activities, runner, sandboxes)]
        for worker in workers:
            await stack.enter_async_context(worker)
        for project in projects:
            await set_current_version(client, project)  # failing here beats runs that never start
        await delete_leftover_schedule(client)
        queues = [CONTROL_QUEUE, runner.task_queue, *(p.task_queue for p in projects)]
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
