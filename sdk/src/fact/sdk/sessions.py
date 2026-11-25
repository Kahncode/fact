"""
When `FactWorkflow.agent` resumes an agent session: decisions made from a step's result or its activity failure.

A session is resumed at most `MAX_RESUMES` times per `agent` call, and only when either:

- the worker stopped or died during the session (the agent did not fail, fact did), or
- the agent answered `blocked` with `retryable: true`, after `RESUME_DELAY`.

Everything else (no answer, the turn limit, a timeout, a blocked answer that is not retryable) is final.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError, TimeoutType
from temporalio.exceptions import TimeoutError as TemporalTimeoutError

with workflow.unsafe.imports_passed_through():
    from fact.core.activity_contract import AGENT_INTERRUPTED
    from fact.core.enums import StepOutcome
    from fact.core.types import StepResult

MAX_RESUMES = 1
RESUME_DELAY = timedelta(minutes=2)  # a durable timer: a temporary problem gets time to pass
INTERRUPTED = "the fact worker running it stopped."


def interrupted_session(exc: ActivityError) -> str | None:
    """Return the session id to resume when the worker stopped (or died) during an agent step, else None."""
    cause = exc.cause
    if isinstance(cause, ApplicationError) and cause.type == AGENT_INTERRUPTED:
        details = list(cause.details)
    elif isinstance(cause, TemporalTimeoutError) and cause.type == TimeoutType.HEARTBEAT:
        details = list(cause.last_heartbeat_details)[1:]  # heartbeats carry (progress, session_id)
    else:
        return None
    session = details[0] if details else None
    return session if isinstance(session, str) and session else None


def retry_reason(result: StepResult) -> str | None:
    """Return why the agent says a later attempt may succeed, or None when its result is final."""
    if result.outcome != StepOutcome.NEEDS_HUMAN or result.outputs.get("retryable") is not True:
        return None
    if not result.session_id:
        return None
    return str(result.outputs.get("blocker") or result.summary)
