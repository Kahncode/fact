"""When an agent session is resumed: `interrupted_session` and `retry_reason`."""

from typing import Any

import pytest
from fact.core.activity_contract import AGENT_INTERRUPTED
from fact.sdk.sessions import interrupted_session, retry_reason
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
