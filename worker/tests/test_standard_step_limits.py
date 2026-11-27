"""`Standard` passes its project's routing and step limits, over the SDK's defaults, to every step it runs."""

from fact.core.activity_contract import AGENT_STEP, SHELL_STEP
from fact.core.step_defaults import AgentStepDefaults, ShellStepDefaults, StepDefaultsConfig
from fact.core.types import Route, RoutingConfig
from fact.sdk.step_defaults import SDK_STEP_DEFAULTS
from standard_harness import FakeActivities, run_standard
from standard_inputs import run_input

MAX_TURNS = 7
SHELL_MINUTES = 5


async def test_steps_get_the_projects_routing_and_limits() -> None:
    routing = RoutingConfig(default=Route(harness="claude-code", model="haiku"))
    limits = StepDefaultsConfig(
        agent=AgentStepDefaults(max_turns=MAX_TURNS), shell=ShellStepDefaults(timeout_minutes=SHELL_MINUTES)
    )
    fakes = FakeActivities()
    await run_standard(
        fakes, "run-limits", run_input().model_copy(update={"routing": routing, "step_defaults": limits})
    )
    agents = fakes.of(AGENT_STEP)
    assert agents
    sdk = SDK_STEP_DEFAULTS.agent
    assert all(a.routing == routing for a in agents)
    assert {(a.max_turns, a.permission_mode) for a in agents} == {(MAX_TURNS, sdk.permission_mode)}
    assert {s.timeout_s for s in fakes.of(SHELL_STEP)} == {SHELL_MINUTES * 60}
