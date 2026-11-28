"""
`python -m fact.worker [--generation ID]`: run the workers until interrupted.

With `--generation`, it serves that worker generation (`generation.py`): each project at the generation's home SHA,
and only the newest generation makes its versions current. Without it, every project at its head, made current.
One workflow worker per served project on `wf-<project>` (versioned and pinned, see `deployments.py`), and for
each the `control` and workspace activity workers of the same version (`activity_workers.py`): every activity on the
queue of its spec's zone, the workspace ones on the runner's queue (`sandbox`). Every activity heartbeats through
`heartbeats.HeartbeatInterceptor`.
Startup deletes a leftover `fact-scheduler` Schedule (`scheduler.py`): the scheduler ticks in the API now. While runs
are active the worker keeps Windows awake (`keep_awake.py`).
Started by `fact dev`, it stops once its stop file appears (`fact.core.stop_file`): like Ctrl-C, the main task is
cancelled and the workers shut down gracefully, so a running agent step ends `AGENT_INTERRUPTED`.
"""

import argparse
import asyncio
import signal
import sys
from contextlib import AsyncExitStack

import httpx
from fact.core import generations
from fact.core.activity_contract import CONTROL_QUEUE
from fact.core.github import GitHubApp, GitHubAppConfigError, shared_app
from fact.core.logging import configure_logging, get_logger
from fact.core.settings import Settings, get_settings
from fact.core.stop_file import stop_file_path, wait_for_stop_file
from fact.worker.activities.agent import AgentActivities
from fact.worker.activities.answer_wait import AnswerWaitActivities
from fact.worker.activities.change_set import ChangeSetActivities
from fact.worker.activities.ci import CIActivities
from fact.worker.activities.classify import ClassifyActivities
from fact.worker.activities.control import ControlActivities
from fact.worker.activities.conversation import ConversationActivities
from fact.worker.activities.cost_comment import CostCommentActivities
from fact.worker.activities.gates import GateActivities
from fact.worker.activities.handoff import HandoffActivities
from fact.worker.activities.merge import MergeActivities
from fact.worker.activities.notify import NotifyActivities
from fact.worker.activities.review_wait import ReviewWaitActivities
from fact.worker.activities.shell import ShellActivities
from fact.worker.activities.task_status import TaskStatusActivities
from fact.worker.activities.workspace import WorkspaceActivities
from fact.worker.activity_workers import Activity, activity_workers
from fact.worker.client import api_http, connect
from fact.worker.deployments import deployment_config
from fact.worker.generation import make_current, serve
from fact.worker.harnesses import ClaudeCodeHarness
from fact.worker.keep_awake import KeepAwake, api_active_runs, watch_active_runs
from fact.worker.loader import ProjectWorkflows, sandbox_runner
from fact.worker.reporting import StepReporter
from fact.worker.runners import LocalRunner
from fact.worker.scheduler import delete_leftover_schedule
from temporalio.client import Client
from temporalio.worker import Worker

HTTP_TIMEOUT_S = 30.0
_background: set[asyncio.Task[None]] = set()  # keeps the stop-file watch referenced while it runs


def _cancel_on_ctrl_break() -> None:
    """On Windows, `fact dev` stops children with Ctrl-Break; drain the workers as for Ctrl-C."""
    if sys.platform != "win32":
        return
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    if task is not None:
        signal.signal(signal.SIGBREAK, lambda *_: loop.call_soon_threadsafe(task.cancel))


def _cancel_on_stop_file() -> None:
    """Drain the workers as for Ctrl-C once `fact dev` writes this process's stop file."""
    path = stop_file_path()
    task = asyncio.current_task()
    if path is None or task is None:
        return

    async def watch() -> None:
        await wait_for_stop_file(path)
        get_logger("fact.worker").info("stop_file_found", path=str(path))
        task.cancel()

    watcher = asyncio.get_running_loop().create_task(watch())
    _background.add(watcher)
    watcher.add_done_callback(_background.discard)


def _github_app(settings: Settings) -> GitHubApp | None:
    """Build the process's one GitHub App, or None when it is not configured (pushing and PRs then fail)."""
    try:
        return shared_app(settings)
    except GitHubAppConfigError as exc:
        get_logger("fact.worker").warning("github_app_unavailable", error=str(exc))
        return None


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
    conversation = ConversationActivities(github)
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
        ReviewWaitActivities(github).wait_for_review,
        conversation.collect_comments,
        conversation.post_replies,
        conversation.request_review,
        conversation.app_slug,
        AnswerWaitActivities(github).wait_for_answer,
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
) -> list[Worker]:
    workers = activity_workers(client, project, activities, workspace_queue=runner.task_queue)
    return [*workers, workflow_worker(client, project)]


async def run(generation: str | None = None) -> None:
    _cancel_on_ctrl_break()
    _cancel_on_stop_file()
    settings = get_settings()
    configure_logging(settings.env)
    client = await connect(settings)
    log = get_logger("fact.worker")
    github = _github_app(settings)
    runner = LocalRunner(settings, github=github)
    served = await serve(settings, runner, generation)
    projects = served.projects
    async with api_http(HTTP_TIMEOUT_S) as http, AsyncExitStack() as stack:
        activities = factory_activities(settings, http, runner, github)
        workers = [w for p in projects for w in _project_workers(client, p, activities, runner)]
        for worker in workers:
            await stack.enter_async_context(worker)
        await make_current(client, served)
        if generation is not None:  # `fact dev` takes a newest generation exiting before this for a failed upgrade
            generations.mark_started(generations.generations_dir(settings.data_dir), generation)
        await delete_leftover_schedule(client)
        queues = [CONTROL_QUEUE, runner.task_queue, *(p.task_queue for p in projects)]
        log.info(
            "worker_started",
            generation=generation,
            current=served.current,
            queues=queues,
            address=settings.temporal.address,
        )
        # on this (the event-loop) thread: the keep-awake hold lasts only while its calling thread lives
        keeper = KeepAwake(enabled=settings.worker.keep_awake)
        await watch_active_runs(keeper, api_active_runs(settings.api_url, http))  # until cancelled


def _args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m fact.worker")
    parser.add_argument("--generation", help="the worker generation to serve (`<data_dir>/run/generations/<id>.json`)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _args(argv)
    try:
        asyncio.run(run(args.generation))
    except KeyboardInterrupt, asyncio.CancelledError:
        get_logger("fact.worker").info("worker_stopped")


if __name__ == "__main__":
    main()
