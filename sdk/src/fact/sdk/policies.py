"""Activity timeouts and retry policies shared by the workflow helpers."""

from datetime import timedelta

from temporalio.common import RetryPolicy

HEARTBEAT_TIMEOUT = timedelta(minutes=2)  # agent, shell and CI steps heartbeat every 30 s
NO_RETRY = RetryPolicy(maximum_attempts=1)  # agent sessions are resumed only by `FactWorkflow.agent`, deliberately
INFRA_RETRY = RetryPolicy(  # shell and control steps: only infrastructure errors raise, and those may pass on retry
    initial_interval=timedelta(seconds=5), maximum_interval=timedelta(minutes=2), maximum_attempts=5
)
LONG_WAIT_RETRY = RetryPolicy(  # a wait of days outlives worker restarts: retry until its schedule-to-close timeout
    initial_interval=timedelta(seconds=5), maximum_interval=timedelta(minutes=2), maximum_attempts=0
)
