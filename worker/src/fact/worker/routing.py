"""Pick the harness and model of an agent step from the routing table in `fact.yaml`."""

from fact.core.settings import RoutingSettings, get_settings
from fact.core.types import Route, RouteOverride

WILDCARD = "*"


def candidates(
    complexity: str, stage: str, routing: RoutingSettings, override: RouteOverride | None = None
) -> list[RouteOverride]:
    """Return the matching entries, most specific first: override, `<complexity>.<stage>`, `<complexity>.*`, `*.<stage>`."""
    keys = (f"{complexity}.{stage}", f"{complexity}.{WILDCARD}", f"{WILDCARD}.{stage}")
    found = [routing.rules[k] for k in keys if k in routing.rules]
    return [override, *found] if override is not None else found


def choose(
    complexity: str,
    stage: str,
    override: RouteOverride | None = None,
    routing: RoutingSettings | None = None,
) -> Route:
    """
    Return the route for a step. Each field (harness, model) comes from the most specific entry that sets it, else the default.

    `routing` defaults to `get_settings().routing`.
    """
    table = routing or get_settings().routing
    chain = candidates(complexity, stage, table, override)
    harness = next((c.harness for c in chain if c.harness), table.default.harness)
    model = next((c.model for c in chain if c.model), table.default.model)
    return Route(harness=harness, model=model)
