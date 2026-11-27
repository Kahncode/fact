"""Pick the harness and model of an agent step from its project's routing table (`.fact/project.yaml`)."""

from fact.core.types import Route, RouteOverride, RoutingConfig

WILDCARD = "*"


def candidates(
    complexity: str, stage: str, routing: RoutingConfig, override: RouteOverride | None = None
) -> list[RouteOverride]:
    """Return the matching entries, most specific first: override, `<complexity>.<stage>`, `<complexity>.*`, `*.<stage>`."""
    keys = (f"{complexity}.{stage}", f"{complexity}.{WILDCARD}", f"{WILDCARD}.{stage}")
    found = [routing.rules[k] for k in keys if k in routing.rules]
    return [override, *found] if override is not None else found


def choose(complexity: str, stage: str, routing: RoutingConfig, override: RouteOverride | None = None) -> Route:
    """Return the route for a step. Each field (harness, model) comes from the most specific entry that sets it, else the default."""
    chain = candidates(complexity, stage, routing, override)
    harness = next((c.harness for c in chain if c.harness), routing.default.harness)
    model = next((c.model for c in chain if c.model), routing.default.model)
    return Route(harness=harness, model=model)
