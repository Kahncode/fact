"""Classify's decision: every signal forces `human_review`; with none, the change set is `auto_merge`."""

import pytest
from fact.core.classification import ChangedPath, ClassifySignal, SecurityFinding
from fact.core.enums import StepOutcome
from fact.core.types import StepResult
from fact.sdk.classification import ClassifyAnswer, decide, parse_classify

CLEAR = ClassifyAnswer(risk="low", risk_reason="small", door="two_way", door_reason="a revert undoes it")


def _signal(name: str) -> ClassifySignal:
    return ClassifySignal(name=name, paths=[ChangedPath(repo="acme/widget", path="api/openapi.json")])  # type: ignore[arg-type]


def test_all_clear_is_auto_merge() -> None:
    result = decide([], CLEAR, spec_ambiguous=False)
    assert (result.route, result.reasons) == ("auto_merge", [])
    assert (result.risk, result.door) == ("low", "two_way")


@pytest.mark.parametrize("name", ["schema_change", "api_change", "api_breaking", "always_review", "task_closed"])
def test_each_rule_signal_forces_review(name: str) -> None:
    result = decide([_signal(name)], CLEAR, spec_ambiguous=False)
    assert result.route == "human_review"
    assert result.reasons[0].endswith("`api/openapi.json`")


@pytest.mark.parametrize(
    ("answer", "spec_ambiguous", "reason"),
    [
        (CLEAR, True, "the implementation found the spec ambiguous"),
        (CLEAR.model_copy(update={"risk": "high", "risk_reason": "touches auth"}), False, "high risk: touches auth"),
        (
            CLEAR.model_copy(update={"door": "one_way", "door_reason": "a migration"}),
            False,
            "a one-way door: a migration",
        ),
        (
            CLEAR.model_copy(update={"security_findings": [SecurityFinding(message="sql")]}),
            False,
            "1 security finding(s)",
        ),
        ("the classify session ended failed_error: boom", False, "the classify session ended failed_error: boom"),
    ],
)
def test_each_judgment_forces_review(
    answer: ClassifyAnswer | str, spec_ambiguous: bool, reason: str  # noqa: FBT001
) -> None:
    result = decide([], answer, spec_ambiguous=spec_ambiguous)
    assert (result.route, result.reasons) == ("human_review", [reason])


def test_unreadable_signals_force_review() -> None:
    result = decide([], CLEAR, spec_ambiguous=False, problems=["the rules' signals could not be read"])
    assert result.route == "human_review"


def test_a_session_without_a_valid_answer_gives_no_judgment() -> None:
    passed = StepResult(outcome=StepOutcome.PASSED, outputs={"risk": "medium"})
    answer = parse_classify(passed)
    assert isinstance(answer, str)
    result = decide([], answer, spec_ambiguous=False)
    assert (result.route, result.risk, result.door) == ("human_review", None, None)
