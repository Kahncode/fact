import pytest
from fact.core import types as core_types
from fact.core.enums import RunOutcome, StepOutcome
from fact.sdk import types
from fact.sdk.types import (
    PRRef,
    RepoCheckout,
    RunInput,
    RunResult,
    StepResult,
    Task,
    Usage,
    WorkspaceHandle,
    WorkspaceSpec,
)
from pydantic import BaseModel, ValidationError
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

MODELS: list[BaseModel] = [
    _TASK,
    _CHECKOUT,
    WorkspaceSpec(run_id="r1", task_key="task:M1-01", repos=[_CHECKOUT]),
    WorkspaceHandle(run_id="r1", root="/w", repos={"o/r": "/w/r"}),
    Usage(tokens_in=1, tokens_out=2, cost_usd=0.5),
    StepResult(
        outcome=StepOutcome.FAILED_CHECKS,
        summary="s",
        outputs={"k": [1, "x", None]},
        log_ref="logs/1",
        transcript_ref="tr/1",
        usage=Usage(tokens_in=1),
    ),
    PRRef(repo="o/r", number=3, url="https://x/pr/3", head_sha="abc"),
    RunInput(run_id="r1", project="fact", task=_TASK, repos=[_CHECKOUT], complexity="complex"),
    RunResult(
        outcome=RunOutcome.MERGED,
        prs=[PRRef(repo="o/r", number=3, url="u", head_sha="abc")],
        summary="done",
    ),
]


@pytest.mark.parametrize("model", MODELS, ids=lambda m: type(m).__name__)
async def test_round_trip_through_temporal_converter(model: BaseModel) -> None:
    converter = pydantic_data_converter
    payloads = await converter.encode([model])
    [decoded] = await converter.decode(payloads, [type(model)])
    assert decoded == model


def test_summary_is_capped() -> None:
    with pytest.raises(ValidationError):
        StepResult(outcome=StepOutcome.PASSED, summary="x" * (types.SUMMARY_MAX_BYTES + 1))


def test_step_result_has_refs_not_raw_logs() -> None:
    assert {"log_ref", "transcript_ref"} <= set(StepResult.model_fields)
    assert not {"log", "transcript", "diff"} & set(StepResult.model_fields)


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
