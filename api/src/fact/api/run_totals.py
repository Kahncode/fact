"""Run totals computed from the run's steps."""

from datetime import UTC, datetime
from decimal import Decimal

from fact.core.enums import StepOutcome, StepStatus
from fact.core.models import Run, Step
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def _aware(value: datetime) -> datetime:
    # SQLite returns naive datetimes; they were stored as UTC.
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def step_machine_seconds(step: Step) -> float:
    """Wall time of a step minus the human wait inside it (a gate's wait is not machine time)."""
    if step.started_at is None or step.ended_at is None:
        return 0.0
    elapsed = (_aware(step.ended_at) - _aware(step.started_at)).total_seconds()
    return max(elapsed - step.wait_seconds, 0.0)


async def recompute_totals(session: AsyncSession, run: Run) -> None:
    steps = (await session.scalars(select(Step).where(Step.run_id == run.id))).all()
    finished = [s for s in steps if s.status == StepStatus.FINISHED]
    run.machine_seconds = sum(step_machine_seconds(s) for s in finished)
    run.human_wait_seconds = sum(s.wait_seconds for s in finished)
    run.tokens_in = sum(s.tokens_in for s in finished)
    run.tokens_out = sum(s.tokens_out for s in finished)
    run.cache_read_tokens = sum(s.cache_read_tokens for s in finished)
    run.cache_write_tokens = sum(s.cache_write_tokens for s in finished)
    # only costs a harness reported; a step that ran an agent without one is counted, never guessed
    run.cost_usd = sum((Decimal(s.cost_usd) for s in finished if s.cost_usd is not None), Decimal(0))
    run.cost_unknown_steps = sum(1 for s in finished if s.harness is not None and s.cost_usd is None)


async def close_running_steps(session: AsyncSession, run: Run) -> None:
    """End the steps a finished run left `running` as `failed_error`: they lost their worker."""
    stuck = (await session.scalars(select(Step).where(Step.run_id == run.id, Step.status == StepStatus.RUNNING))).all()
    for step in stuck:
        step.status = StepStatus.FINISHED
        step.outcome = StepOutcome.FAILED_ERROR
        step.ended_at = run.ended_at
    if stuck:
        await session.flush()
        await recompute_totals(session, run)
