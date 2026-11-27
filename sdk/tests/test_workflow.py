import pytest
from fact.sdk.failures import ci_failure_context
from fact.sdk.types import CheckResult, PRRef, StepKind, StepOutcome, StepResult
from fact.sdk.workflow import CONTEXT_MAX_BYTES, FactWorkflow, Stage, failure_context, stages


def test_stages_stores_the_manifest_as_data() -> None:
    @stages([Stage("a", "A", StepKind.AGENT), Stage("b", "B", StepKind.SHELL, loops_to="a")])
    class Flow(FactWorkflow):
        pass

    assert Flow.stage_manifest == (Stage("a", "A", StepKind.AGENT), Stage("b", "B", StepKind.SHELL, loops_to="a"))
    assert Flow.manifest() == [
        {"id": "a", "label": "A", "kind": "agent", "loops_to": None},
        {"id": "b", "label": "B", "kind": "shell", "loops_to": "a"},
    ]
    assert FactWorkflow.manifest() == []  # the base class keeps its own empty manifest


def test_stages_refuses_duplicate_ids() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        stages([Stage("a", "A", StepKind.AGENT), Stage("a", "Again", StepKind.SHELL)])


def test_stages_refuses_an_unknown_loop_target() -> None:
    with pytest.raises(ValueError, match="unknown stage 'nope'"):
        stages([Stage("a", "A", StepKind.AGENT, loops_to="nope")])


def test_attempts_are_numbered_per_stage() -> None:
    flow = FactWorkflow()
    assert [flow.next_attempt("checks"), flow.next_attempt("implement"), flow.next_attempt("checks")] == [1, 1, 2]


def test_input_before_start_is_an_error() -> None:
    with pytest.raises(RuntimeError, match="has not started"):
        _ = FactWorkflow().input


def _failed(name: str, outputs: dict) -> tuple[CheckResult, StepResult]:
    step = StepResult(outcome=StepOutcome.FAILED_CHECKS, summary="it broke", outputs=outputs)
    return CheckResult(name=name, outcome=step.outcome, summary=step.summary), step


def test_failure_context_shows_command_and_output() -> None:
    text = failure_context(
        [
            _failed("lint", {"command": ["ruff", "check", "."], "exit_code": 1, "failure_output": "E501 too long"}),
            _failed("test", {}),
        ]
    )
    for shown in ("lint", "ruff check .", "exited with 1", "E501 too long", "test", "it broke"):
        assert shown in text


def test_failure_context_is_capped() -> None:
    huge = _failed("test", {"command": ["pytest"], "exit_code": 1, "failure_output": "x" * (2 * CONTEXT_MAX_BYTES)})
    assert len(failure_context([huge]).encode()) == CONTEXT_MAX_BYTES


def test_ci_failure_context_shows_each_failed_job() -> None:
    pr = PRRef(repo="acme/widget", number=7, url="https://github.com/acme/widget/pull/7", head_sha="0123456789abcdef")
    job = {"name": "test (windows)", "conclusion": "failure", "url": "https://ci/42", "log_tail": "E assert 1 == 2"}
    failed = StepResult(outcome=StepOutcome.FAILED_CHECKS, summary="1 failed", outputs={"failures": [job]})
    no_jobs = StepResult(outcome=StepOutcome.FAILED_CHECKS, summary="CI still running")

    text = ci_failure_context([(pr, failed), (pr, no_jobs)])

    for shown in ("test (windows)", "acme/widget#7", "0123456789ab", "https://ci/42", "E assert 1 == 2"):
        assert shown in text
    assert "CI still running" in text
