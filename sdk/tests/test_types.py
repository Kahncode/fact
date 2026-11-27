import pytest
from fact.core import types as core_types
from fact.core.enums import RunOutcome, StepOutcome
from fact.sdk import types
from fact.sdk.types import PRRef, RepoCheckout, RunInput, RunResult, StepResult, Task
from pydantic import ValidationError
from temporalio.contrib.pydantic import pydantic_data_converter

_TASK = Task(
    key="task:M1-01",
    id="M1-01",
    title="Shared types",
    body="body",
    acceptance_criteria=["a", "b"],
    type="bug",
    complexity="complex",
    depends_on=["M0-06"],
    path="tasks/M1/01-shared-types.md",
)
_CHECKOUT = RepoCheckout(full_name="o/r", default_branch="main", branch="fact/M1-01")


async def test_run_payloads_round_trip_through_the_temporal_converter() -> None:
    """The run input and result are what crosses Temporal: nested models and enums survive."""
    run_input = RunInput(run_id="r1", project="fact", task=_TASK, repos=[_CHECKOUT], complexity="complex")
    result = RunResult(
        outcome=RunOutcome.MERGED, prs=[PRRef(repo="o/r", number=3, url="u", head_sha="abc")], summary="done"
    )
    for model in (run_input, result):
        [decoded] = await pydantic_data_converter.decode(await pydantic_data_converter.encode([model]), [type(model)])
        assert decoded == model


def test_summary_is_capped() -> None:
    with pytest.raises(ValidationError):
        StepResult(outcome=StepOutcome.PASSED, summary="x" * (types.SUMMARY_MAX_BYTES + 1))


def test_summary_cap_counts_bytes() -> None:
    with pytest.raises(ValidationError):
        StepResult(outcome=StepOutcome.PASSED, summary="€" * 700)  # 700 chars, 2100 bytes
    with pytest.raises(ValidationError):
        RunResult(outcome=RunOutcome.FAILED, summary="€" * 700)
    StepResult(outcome=StepOutcome.PASSED, summary="€" * 682)


def test_sdk_reexports_core_types() -> None:
    public = {
        n
        for n, v in vars(core_types).items()
        if not n.startswith("_") and getattr(v, "__module__", "") == core_types.__name__
    }
    assert public <= set(types.__all__)
