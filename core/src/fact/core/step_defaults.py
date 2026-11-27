"""
A project's step limits (`step_defaults` in `.fact/project.yaml`), overriding the SDK's defaults per value.

Every field here is unset (None) unless the project sets it; `fact.sdk.step_defaults` holds the defaults in code
and `resolve` fills in what the project leaves out. `fact.core.project_config` re-exports these.
"""

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["AgentStepDefaults", "ShellStepDefaults", "StepDefaults", "StepDefaultsConfig"]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StepDefaults(_Section):
    timeout_minutes: float | None = Field(default=None, gt=0)  # the activity's start-to-close timeout


class AgentStepDefaults(StepDefaults):
    max_turns: int | None = Field(default=None, ge=1)  # caps the agent's turns
    # Harness permission mode, such as Claude Code's "auto" (a classifier approves or denies, nobody is asked) or
    # "bypassPermissions" (checks off: only in an isolated runner).
    permission_mode: str | None = None


class ShellStepDefaults(StepDefaults):
    pass


class StepDefaultsConfig(_Section):
    """Limits per step kind. Typed fields, not a dict, so setting one value keeps the others' defaults."""

    agent: AgentStepDefaults = Field(default_factory=AgentStepDefaults)
    shell: ShellStepDefaults = Field(default_factory=ShellStepDefaults)

    def over(self, defaults: StepDefaultsConfig) -> StepDefaultsConfig:
        """Return `defaults` with every value this config sets replaced by it."""
        return StepDefaultsConfig(
            agent=defaults.agent.model_copy(update=self.agent.model_dump(exclude_none=True)),
            shell=defaults.shell.model_copy(update=self.shell.model_dump(exclude_none=True)),
        )
