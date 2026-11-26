"""
What a person sends a running workflow, and what it tells them: gates, steering and the status query.

The handler names are the contract: the API and the CLI call them by name, so they never import `fact.sdk`.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from fact.core.enums import StepOutcome
from pydantic import BaseModel, ConfigDict, Field

ANSWER_GATE = "answer_gate"  # update: `GateAnswer` in, nothing out; refused if the gate is not open
STATUS = "status"  # query: `WorkflowStatus`
STEER = "steer"  # signal: extra instructions (str) for the next agent session

NOTE_MAX_CHARS = 4000
STEER_MAX_CHARS = 8000


class GateDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class GateAnswer(_Model):
    gate_id: str
    decision: GateDecision
    note: str = Field(default="", max_length=NOTE_MAX_CHARS)


class OpenGate(_Model):
    gate_id: str
    kind: str  # such as `needs_human` or `plan_approval`
    stage_id: str  # the gate's step is this stage, at `attempt`
    attempt: int
    details: dict[str, Any] = Field(default_factory=dict)  # small and JSON-serializable, for the person deciding
    opened_at: datetime


class StageOutcome(_Model):
    stage_id: str
    attempt: int
    outcome: StepOutcome


class WorkflowStatus(_Model):
    stage: str | None = None  # the stage of the latest step, running or done; None before the first
    attempt: int | None = None
    gate: OpenGate | None = None  # the gate waiting for an answer, if any
    outcomes: list[StageOutcome] = Field(default_factory=list)  # every step that ended, in order
