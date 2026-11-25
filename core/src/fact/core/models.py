"""
ORM models: projects, repositories, runs, steps, events.

Only Postgres-compatible column types. Enum values are stored as plain strings (see `fact.core.enums`).
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fact.core.db import Base
from fact.core.enums import RunStatus, StepStatus
from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

USD = Numeric(12, 6)


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


def _ts(*, nullable: bool = True, default_now: bool = False) -> Mapped[Any]:
    return mapped_column(DateTime(timezone=True), nullable=nullable, default=utcnow if default_now else None)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    home_repo: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)


class Repository(Base):
    __tablename__ = "repositories"
    __table_args__ = (UniqueConstraint("project_id", "full_name", name="uq_repositories_project_full_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    full_name: Mapped[str] = mapped_column(String(300))
    role: Mapped[str | None] = mapped_column(String(50))
    default_branch: Mapped[str] = mapped_column(String(200), default="main")
    merge_order: Mapped[int] = mapped_column(Integer, default=0)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workflow_id: Mapped[str] = mapped_column(String(200), unique=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    task_key: Mapped[str] = mapped_column(String(200), index=True)
    workflow_type: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default=RunStatus.QUEUED, index=True)
    outcome: Mapped[str | None] = mapped_column(String(20))
    complexity: Mapped[str | None] = mapped_column(String(50))
    worker_version: Mapped[str | None] = mapped_column(String(64))
    home_sha: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    started_at: Mapped[datetime | None] = _ts()
    ended_at: Mapped[datetime | None] = _ts()
    machine_seconds: Mapped[float] = mapped_column(Float, default=0)
    human_wait_seconds: Mapped[float] = mapped_column(Float, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(USD, default=Decimal(0))
    human_touches: Mapped[int] = mapped_column(Integer, default=0)


class Step(Base):
    __tablename__ = "steps"
    __table_args__ = (UniqueConstraint("run_id", "stage_id", "attempt", name="uq_steps_run_stage_attempt"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    stage_id: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(20))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default=StepStatus.QUEUED)
    outcome: Mapped[str | None] = mapped_column(String(30))
    harness: Mapped[str | None] = mapped_column(String(50))
    model: Mapped[str | None] = mapped_column(String(100))
    queued_at: Mapped[datetime | None] = _ts()
    started_at: Mapped[datetime | None] = _ts()
    ended_at: Mapped[datetime | None] = _ts()
    wait_seconds: Mapped[float] = mapped_column(Float, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(USD, default=Decimal(0))
    log_ref: Mapped[str | None] = mapped_column(Text)
    transcript_ref: Mapped[str | None] = mapped_column(Text)
    outputs: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    step_id: Mapped[str | None] = mapped_column(ForeignKey("steps.id"), index=True)
    type: Mapped[str] = mapped_column(String(50))
    at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
