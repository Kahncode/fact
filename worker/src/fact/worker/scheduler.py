"""
The scheduler: the `fact-scheduler` Temporal Schedule starts a short `SchedulerTick` workflow on `control`.

- `SchedulerTick` reads what may start (`find_ready_tasks`), orders it and applies the caps (`plan_starts`), checks
  the Claude usage gate when something would start (`check_claude_usage`: a full window starts nothing), and starts
  each run through the API (`start_scheduled_run`).
- `ensure_schedule` runs at worker start: it creates the Schedule when missing (paused unless
  `concurrency.scheduler_enabled`) and moves its interval to `concurrency.scheduler_interval_seconds`. It never
  pauses or unpauses an existing Schedule: that is `fact scheduler on|off` or the Temporal UI. Only the interval is
  updated: a change to the action or policy here reaches an existing Schedule once it is deleted and recreated.
"""

from dataclasses import replace
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from fact.core.activity_contract import CONTROL_QUEUE
    from fact.core.logging import get_logger
    from fact.core.scheduler import (
        CHECK_CLAUDE_USAGE,
        FIND_READY_TASKS,
        SCHEDULE_ID,
        SCHEDULER_TICK,
        START_SCHEDULED_RUN,
        TICK_WORKFLOW_ID,
        ReadyTasks,
        StartScheduledRunInput,
        TickResult,
        plan_starts,
    )
    from fact.core.settings import Settings
    from fact.core.usage_gate import GateState, UsageCheck, UsageCheckInput
    from temporalio.client import (
        Client,
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleAlreadyRunningError,
        ScheduleIntervalSpec,
        SchedulePolicy,
        ScheduleSpec,
        ScheduleState,
        ScheduleUpdate,
        ScheduleUpdateInput,
    )
    from temporalio.client import ScheduleOverlapPolicy as Overlap
    from temporalio.service import RPCError, RPCStatusCode

FIND_TIMEOUT = timedelta(minutes=3)  # a git fetch per project
START_TIMEOUT = timedelta(minutes=1)
USAGE_TIMEOUT = timedelta(seconds=30)
TICK_TIMEOUT = timedelta(minutes=10)


@workflow.defn(name=SCHEDULER_TICK)
class SchedulerTick:
    @workflow.run
    async def run(self) -> TickResult:
        ready = await workflow.execute_activity(
            FIND_READY_TASKS,
            start_to_close_timeout=FIND_TIMEOUT,
            retry_policy=RetryPolicy(maximum_attempts=2),
            result_type=ReadyTasks,
        )
        chosen = plan_starts(ready)
        if not chosen:
            return TickResult()
        if reason := await self._usage_full():
            workflow.logger.info("usage window full, starting nothing: %s", reason)
            return TickResult(usage_full=reason)
        started: list[str] = []
        failed: list[str] = []
        for task in chosen:
            try:
                workflow_id = await workflow.execute_activity(
                    START_SCHEDULED_RUN,
                    StartScheduledRunInput(project=task.project, task_key=task.task_key),
                    start_to_close_timeout=START_TIMEOUT,
                    retry_policy=RetryPolicy(maximum_attempts=1),  # a lost answer may still have started the run
                    result_type=str,
                )
            except ActivityError:
                workflow.logger.warning("scheduled start failed for %s", task.task_key)
                failed.append(task.task_key)
                continue
            if not workflow_id:  # the API refused it
                failed.append(task.task_key)
            else:
                started.append(workflow_id)
        return TickResult(started=started, failed=failed)

    @staticmethod
    async def _usage_full() -> str:
        """Why a full usage window holds every start; "" to start (unreadable usage is an error, not a block)."""
        try:
            usage = await workflow.execute_activity(
                CHECK_CLAUDE_USAGE,
                UsageCheckInput(now=workflow.now()),
                start_to_close_timeout=USAGE_TIMEOUT,
                retry_policy=RetryPolicy(maximum_attempts=1),  # the next tick asks again
                result_type=UsageCheck,
            )
        except ActivityError:
            workflow.logger.exception("usage gate failed; starting runs as usual")
            return ""
        return usage.reason if usage.state == GateState.FULL else ""


def schedule_spec(settings: Settings) -> ScheduleSpec:
    every = timedelta(seconds=settings.concurrency.scheduler_interval_seconds)
    return ScheduleSpec(intervals=[ScheduleIntervalSpec(every=every)])


def build_schedule(settings: Settings) -> Schedule:
    enabled = settings.concurrency.scheduler_enabled
    return Schedule(
        action=ScheduleActionStartWorkflow(
            SCHEDULER_TICK, id=TICK_WORKFLOW_ID, task_queue=CONTROL_QUEUE, execution_timeout=TICK_TIMEOUT
        ),
        spec=schedule_spec(settings),
        policy=SchedulePolicy(overlap=Overlap.SKIP),  # a slow tick never stacks
        state=ScheduleState(paused=not enabled, note=None if enabled else "created paused: `fact scheduler on`"),
    )


async def ensure_schedule(client: Client, settings: Settings) -> str:
    """Create the Schedule if missing, else update its interval if `fact.yaml` changed it; return what was done."""
    log = get_logger("fact.worker")
    handle = client.get_schedule_handle(SCHEDULE_ID)
    try:
        desc = await handle.describe()
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        try:
            await client.create_schedule(SCHEDULE_ID, build_schedule(settings))
        except ScheduleAlreadyRunningError:  # another worker created it meanwhile
            return "unchanged"
        log.info("scheduler_created", paused=not settings.concurrency.scheduler_enabled)
        return "created"
    spec = schedule_spec(settings)
    if [i.every for i in desc.schedule.spec.intervals] == [i.every for i in spec.intervals]:
        return "unchanged"

    def update(current: ScheduleUpdateInput) -> ScheduleUpdate:
        return ScheduleUpdate(schedule=replace(current.description.schedule, spec=spec))

    await handle.update(update)
    log.info("scheduler_interval_updated", seconds=settings.concurrency.scheduler_interval_seconds)
    return "updated"
