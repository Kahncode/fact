"""Request and response models for runs, steps and events."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from fact.core.enums import RunOutcome, RunStatus, RunTrigger, StepKind, StepOutcome, StepStatus
from pydantic import BaseModel, ConfigDict, Field


class RunCreate(BaseModel):
    workflow_id: str
    project: str
    task_key: str
    workflow_type: str
    complexity: str | None = None
    worker_version: str | None = None
    home_sha: str | None = None
    trigger: RunTrigger = RunTrigger.MANUAL


class RunUpdate(BaseModel):
    status: RunStatus | None = None
    outcome: RunOutcome | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    human_touches: int | None = Field(default=None, ge=0)
    worker_version: str | None = Field(default=None, max_length=64)
    home_sha: str | None = Field(default=None, max_length=64)


class StepCreate(BaseModel):
    stage_id: str
    kind: StepKind
    attempt: int = Field(default=1, ge=1)
    queued_at: datetime | None = None
    started_at: datetime | None = None
    harness: str | None = None
    model: str | None = None


class StepUpdate(BaseModel):
    status: StepStatus | None = None
    outcome: StepOutcome | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    wait_seconds: float | None = Field(default=None, ge=0)
    tokens_in: int | None = Field(default=None, ge=0)
    tokens_out: int | None = Field(default=None, ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=0)
    log_ref: str | None = None
    transcript_ref: str | None = None
    outputs: dict[str, Any] | None = None


class EventIn(BaseModel):
    type: str
    at: datetime | None = None
    step_id: str | None = None
    payload: dict[str, Any] | None = None


class StepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    run_id: str
    stage_id: str
    kind: str
    attempt: int
    status: str
    outcome: str | None
    harness: str | None
    model: str | None
    queued_at: datetime | None
    started_at: datetime | None
    ended_at: datetime | None
    wait_seconds: float
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal
    log_ref: str | None
    transcript_ref: str | None
    outputs: dict[str, Any] | None


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    workflow_id: str
    project_id: int
    task_key: str
    workflow_type: str
    status: str
    outcome: str | None
    complexity: str | None
    trigger: str
    worker_version: str | None
    home_sha: str | None
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    machine_seconds: float
    human_wait_seconds: float
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal
    human_touches: int


class RunDetail(RunOut):
    steps: list[StepOut]


class EventsAccepted(BaseModel):
    accepted: int
