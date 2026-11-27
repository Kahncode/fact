"""Routing resolution order."""

from pathlib import Path

import pytest
from fact.core.project_config import load_project_config
from fact.core.types import DEFAULT_ROUTE, Route, RouteOverride, RoutingConfig
from fact.worker.routing import choose
from pydantic import ValidationError

TABLE = RoutingConfig(
    default=Route(harness="claude-code", model="sonnet"),
    rules={
        "complex.implement": RouteOverride(model="exact"),
        "complex.*": RouteOverride(model="complexity-any", harness="other"),
        "*.implement": RouteOverride(model="stage-any"),
        "*.review": RouteOverride(model="review"),
    },
)


def test_override_wins() -> None:
    route = choose("complex", "implement", TABLE, RouteOverride(model="haiku"))
    assert route == Route(harness="other", model="haiku")  # harness falls through to complex.*


def test_exact_key_beats_complexity_wildcard() -> None:
    assert choose("complex", "implement", TABLE).model == "exact"


def test_complexity_wildcard_beats_stage_wildcard() -> None:
    assert choose("complex", "review", TABLE).model == "complexity-any"


def test_stage_wildcard_beats_default() -> None:
    assert choose("standard", "implement", TABLE).model == "stage-any"


def test_default_when_nothing_matches() -> None:
    assert choose("standard", "learn", TABLE) == Route(harness="claude-code", model="sonnet")


def test_fields_fall_through_independently() -> None:
    assert choose("complex", "implement", TABLE).harness == "other"
    assert choose("standard", "review", TABLE).harness == "claude-code"


@pytest.mark.parametrize("key", ["implement", ".implement", "complex.", "a.b.c", "compelx.implement"])
def test_bad_rule_key_is_rejected(key: str) -> None:
    with pytest.raises(ValidationError, match="complexity"):
        RoutingConfig(rules={key: RouteOverride(model="x")})


def test_unknown_rule_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RoutingConfig.model_validate({"rules": {"*.review": {"modle": "opus"}}})


def _project(tmp_path: Path, extra: str = "") -> RoutingConfig:
    (tmp_path / ".fact").mkdir()
    yaml = "name: acme\nrepositories:\n  - full_name: acme/home\n    role: backend\nworkflows:\n  default: standard\n"
    (tmp_path / ".fact" / "project.yaml").write_text(yaml + extra, encoding="utf-8")
    return load_project_config(tmp_path).routing


def test_a_project_without_routing_gets_the_default_route(tmp_path: Path) -> None:
    assert choose("complex", "implement", _project(tmp_path)) == DEFAULT_ROUTE
