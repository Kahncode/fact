"""
When `FactWorkflow.agent` resumes an agent session: decisions made from a step's result or its activity failure.

A session is resumed at most `MAX_RESUMES` times per `agent` call, and only when either:

- the worker stopped or died during the session (the agent did not fail, fact did), or
- the agent answered `blocked` with `retryable: true`, after `RESUME_DELAY`.

Separately, a session the subscription's usage limit refused (`rate_limited`) is resumed once the limit resets, at
its `resume_at`, up to `MAX_LIMIT_PAUSES` times; these pauses do not count against `MAX_RESUMES`.

A session that answered `blocked` with a `question` is resumed once a person answered it (`fact.sdk.asking`, through
`FactWorkflow.agent`), as many times as it asks; this is not a resume these rules count.

Everything else (no answer, the turn limit, a timeout, a blocked answer that is neither retryable nor a question) is
final.
"""

from datetime import UTC, datetime, timedelta

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError, TimeoutType
from temporalio.exceptions import TimeoutError as TemporalTimeoutError

with workflow.unsafe.imports_passed_through():
    from fact.core.activity_contract import AGENT_INTERRUPTED
    from fact.core.claude_usage import LIMIT_FALLBACK
    from fact.core.enums import StepOutcome
    from fact.core.types import StepResult

MAX_RESUMES = 1
RESUME_DELAY = timedelta(minutes=2)  # a durable timer: a temporary problem gets time to pass
INTERRUPTED = "the fact worker running it stopped."
MAX_LIMIT_PAUSES = 20  # per `agent` call; one more limit hit ends the step `needs_human`
LIMIT_PAUSED = "you were paused by the subscription's usage limit, which has now reset."


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


def limit_resume_at(result: StepResult, now: datetime) -> datetime:
    """When to resume a `rate_limited` session: its `resume_at` output (UTC if it has no offset), else `now` + 30 min."""
    try:
        resume_at = datetime.fromisoformat(str(result.outputs.get("resume_at")))
    except ValueError:
        return now + LIMIT_FALLBACK
    return resume_at if resume_at.tzinfo else resume_at.replace(tzinfo=UTC)


def limit_session(result: StepResult) -> str | None:
    """Return the session a `rate_limited` step ran, to resume; None when it never started one."""
    session = result.outputs.get("session_id") or result.session_id
    return session if isinstance(session, str) and session else None
