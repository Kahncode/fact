"""Limits per step kind (`step_defaults` in `fact.yaml`); `fact.core.settings` re-exports these."""

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["AgentStepDefaults", "ShellStepDefaults", "StepDefaults", "StepDefaultsSettings"]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StepDefaults(_Section):
    timeout_minutes: float = Field(default=30.0, gt=0)
    max_turns: int | None = Field(default=None, ge=1)  # agent steps only; None: the harness's own limit


class AgentStepDefaults(StepDefaults):
    timeout_minutes: float = Field(default=60.0, gt=0)
    max_turns: int | None = Field(default=100, ge=1)
    # Harness permission mode, such as Claude Code's "auto" (a classifier approves or denies, nobody is asked) or
    # "bypassPermissions" (checks off: only in an isolated runner). None: the harness default.
    permission_mode: str | None = "auto"


class ShellStepDefaults(StepDefaults):
    timeout_minutes: float = Field(default=15.0, gt=0)


class StepDefaultsSettings(_Section):
    """
    Limits per step kind. Typed fields, not a dict, so setting one value in YAML keeps the others' defaults.

    Kinds without a field get `StepDefaults()`.
    """

    agent: AgentStepDefaults = Field(default_factory=AgentStepDefaults)
    shell: ShellStepDefaults = Field(default_factory=ShellStepDefaults)
