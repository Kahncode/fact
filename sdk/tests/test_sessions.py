"""When an agent session is resumed: `interrupted_session` and `retry_reason`."""

from datetime import UTC, datetime
from typing import Any

import pytest
from fact.core.activity_contract import AGENT_INTERRUPTED
from fact.sdk.sessions import LIMIT_FALLBACK, interrupted_session, limit_resume_at, limit_session, retry_reason
from fact.sdk.types import StepOutcome, StepResult
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    RetryState,
    TimeoutType,
)
from temporalio.exceptions import TimeoutError as TemporalTimeoutError

BLOCKED = StepOutcome.NEEDS_HUMAN


def _activity_error(cause: BaseException) -> ActivityError:
    error = ActivityError(
        "activity failed",
        scheduled_event_id=1,
        started_event_id=2,
        identity="test",
        activity_type="agent_step",
        activity_id="1",
        retry_state=RetryState.MAXIMUM_ATTEMPTS_REACHED,
    )
    error.__cause__ = cause
    return error


def _timeout(kind: TimeoutType, details: list[Any]) -> TemporalTimeoutError:
    return TemporalTimeoutError("timed out", type=kind, last_heartbeat_details=details)


@pytest.mark.parametrize(
    ("cause", "expected"),
    [
        (ApplicationError("worker stopped", "s-1", type=AGENT_INTERRUPTED), "s-1"),
        (ApplicationError("worker stopped", None, type=AGENT_INTERRUPTED), None),  # stopped before the session began
        (ApplicationError("boom", "s-1", type="ValueError"), None),
        (_timeout(TimeoutType.HEARTBEAT, ["implement: 3 turns", "s-2"]), "s-2"),
        (_timeout(TimeoutType.HEARTBEAT, ["implement: 0 turns", None]), None),
        (_timeout(TimeoutType.HEARTBEAT, []), None),  # died before the first heartbeat
        (_timeout(TimeoutType.START_TO_CLOSE, ["implement: 90 turns", "s-3"]), None),  # too slow: not resumed
    ],
)
def test_interrupted_session(cause: BaseException, expected: str | None) -> None:
    assert interrupted_session(_activity_error(cause)) == expected


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            StepResult(outcome=BLOCKED, outputs={"retryable": True, "blocker": "rate limited"}, session_id="s"),
            "rate limited",
        ),
        (StepResult(outcome=BLOCKED, summary="503s", outputs={"retryable": True}, session_id="s"), "503s"),
        (StepResult(outcome=BLOCKED, outputs={"retryable": False}, session_id="s"), None),
        (StepResult(outcome=BLOCKED, outputs={"retryable": "yes"}, session_id="s"), None),  # only a real true
        (StepResult(outcome=BLOCKED, outputs={"retryable": True}), None),  # no session to resume
        (StepResult(outcome=StepOutcome.FAILED_ERROR, outputs={"retryable": True}, session_id="s"), None),
        (StepResult(outcome=StepOutcome.PASSED, outputs={"retryable": True}, session_id="s"), None),
    ],
)
def test_retry_reason(result: StepResult, expected: str | None) -> None:
    assert retry_reason(result) == expected


NOW = datetime(2030, 1, 2, 12, 0, tzinfo=UTC)


def _limited(**outputs: Any) -> StepResult:  # noqa: ANN401
    return StepResult(outcome=StepOutcome.RATE_LIMITED, outputs=outputs, session_id="s-step")


def test_limit_resume_at_reads_the_output() -> None:
    at = datetime(2030, 1, 5, 9, 30, tzinfo=UTC)
    assert limit_resume_at(_limited(resume_at=at.isoformat()), NOW) == at
    assert limit_resume_at(_limited(resume_at="2030-01-05T09:30:00"), NOW) == at  # no offset: UTC


@pytest.mark.parametrize("outputs", [{}, {"resume_at": "soon"}, {"resume_at": None}])
def test_limit_resume_at_falls_back_to_now_plus_30_minutes(outputs: dict[str, Any]) -> None:
    assert limit_resume_at(_limited(**outputs), NOW) == NOW + LIMIT_FALLBACK


def test_limit_session_prefers_the_output_then_the_step() -> None:
    assert limit_session(_limited(session_id="s-out")) == "s-out"
    assert limit_session(_limited()) == "s-step"
    assert limit_session(StepResult(outcome=StepOutcome.RATE_LIMITED)) is None
