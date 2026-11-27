"""
The step limits every run starts from; a project's `step_defaults` in `.fact/project.yaml` overrides any value.

A run carries its project's overrides (`RunInput.step_defaults`); `resolve` fills in the values it leaves out.
"""

from fact.core.step_defaults import AgentStepDefaults, ShellStepDefaults, StepDefaultsConfig

SECONDS_PER_MINUTE = 60

# permission_mode "auto": Claude Code keeps its checks on and a classifier decides, without prompting anyone.
SDK_STEP_DEFAULTS = StepDefaultsConfig(
    agent=AgentStepDefaults(timeout_minutes=60.0, max_turns=100, permission_mode="auto"),
    shell=ShellStepDefaults(timeout_minutes=15.0),
)


def resolve(project: StepDefaultsConfig) -> StepDefaultsConfig:
    """Return the SDK's defaults with every value the project sets replaced by it; every field is set."""
    return project.over(SDK_STEP_DEFAULTS)


def timeout_s(limits: AgentStepDefaults | ShellStepDefaults) -> float:
    """Return the step's timeout in seconds, from a resolved config."""
    if limits.timeout_minutes is None:
        msg = "step limits are not resolved: timeout_minutes is unset"
        raise ValueError(msg)
    return limits.timeout_minutes * SECONDS_PER_MINUTE
