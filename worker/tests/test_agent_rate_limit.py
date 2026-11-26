"""agent_step when the usage limit refuses the session: outcome `rate_limited`, and the windows reach the usage API."""

# ruff: noqa: F811 - the tests take the fixtures imported from test_agent_step by name
from datetime import UTC, datetime, timedelta

from fact.core.claude_usage import ClaudeUsage, UsageWindow
from fact.core.enums import StepOutcome
from fact.worker.activities.agent import LIMIT_FALLBACK, rate_limited
from test_agent_step import Env, FakeHarness, _result, agent_settings, env, handle  # noqa: F401 - fixtures

RESETS = datetime(2030, 1, 2, 15, 0, tzinfo=UTC)
FULL = UsageWindow(name="five_hour", utilization=1.0, resets_at=RESETS, rejected=True)
WEEK = UsageWindow(name="seven_day", utilization=0.5, resets_at=RESETS + timedelta(days=3))
NOW = datetime(2030, 1, 2, 12, 0, tzinfo=UTC)


def _limited(windows: tuple[UsageWindow, ...]) -> FakeHarness:
    result = _result("success", is_error=True, answer=None).model_copy(
        update={
            "final_message": "usage limit reached",
            "rate_limited": True,
            "usage_windows": ClaudeUsage(windows=windows),
        }
    )
    return FakeHarness(result)


async def test_rate_limited_session_resumes_at_the_reset_and_reports_windows(env: Env) -> None:
    result = await env.run(_limited((FULL, WEEK)))

    assert result.outcome == StepOutcome.RATE_LIMITED
    assert result.outputs == {"resume_at": (RESETS + timedelta(seconds=90)).isoformat(), "session_id": "s-1"}
    assert result.session_id == "s-1"
    step = await env.step()
    assert (step["outcome"], step["outputs"]) == ("rate_limited", result.outputs)

    usage = (await env.client.get("/api/v1/usage")).json()
    assert [(u["account"], u["window"], u["utilization"]) for u in usage] == [
        ("claude", "five_hour", 1.0),
        ("claude", "seven_day", 0.5),
    ]


async def test_windows_are_reported_for_a_session_that_passed(env: Env) -> None:
    harness = FakeHarness(_result().model_copy(update={"usage_windows": ClaudeUsage(windows=(WEEK,))}))
    assert (await env.run(harness)).outcome == StepOutcome.PASSED
    assert [u["window"] for u in (await env.client.get("/api/v1/usage")).json()] == ["seven_day"]


async def test_no_windows_reports_nothing(env: Env) -> None:
    assert (await env.run(FakeHarness())).outcome == StepOutcome.PASSED
    assert (await env.client.get("/api/v1/usage")).json() == []


def test_limit_without_a_known_reset_waits_the_fallback() -> None:
    result = _result(is_error=True).model_copy(update={"rate_limited": True, "session_id": None})
    _, outputs = rate_limited(result, "s-progress", NOW)
    assert outputs == {"resume_at": (NOW + LIMIT_FALLBACK).isoformat(), "session_id": "s-progress"}


def test_limit_whose_reset_has_passed_waits_the_fallback() -> None:
    past = FULL.model_copy(update={"resets_at": NOW - timedelta(hours=1)})
    result = _result(is_error=True).model_copy(
        update={"rate_limited": True, "usage_windows": ClaudeUsage(windows=(past,))}
    )
    _, outputs = rate_limited(result, None, NOW)
    assert outputs["resume_at"] == (NOW + LIMIT_FALLBACK).isoformat()
