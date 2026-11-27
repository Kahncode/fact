"""Routing resolution order."""

import pytest
from fact.core.settings import RoutingSettings
from fact.core.types import Route, RouteOverride
from fact.worker.routing import choose
from pydantic import ValidationError

TABLE = RoutingSettings(
    default=Route(harness="claude-code", model="sonnet"),
    rules={
        "complex.implement": RouteOverride(model="exact"),
        "complex.*": RouteOverride(model="complexity-any", harness="other"),
        "*.implement": RouteOverride(model="stage-any"),
        "*.review": RouteOverride(model="review"),
    },
)


def test_override_wins() -> None:
    route = choose("complex", "implement", RouteOverride(model="haiku"), routing=TABLE)
    assert route == Route(harness="other", model="haiku")  # harness falls through to complex.*


def test_exact_key_beats_complexity_wildcard() -> None:
    assert choose("complex", "implement", routing=TABLE).model == "exact"


def test_complexity_wildcard_beats_stage_wildcard() -> None:
    assert choose("complex", "review", routing=TABLE).model == "complexity-any"


def test_stage_wildcard_beats_default() -> None:
    assert choose("standard", "implement", routing=TABLE).model == "stage-any"


def test_default_when_nothing_matches() -> None:
    assert choose("standard", "learn", routing=TABLE) == Route(harness="claude-code", model="sonnet")


def test_fields_fall_through_independently() -> None:
    assert choose("complex", "implement", routing=TABLE).harness == "other"
    assert choose("standard", "review", routing=TABLE).harness == "claude-code"


@pytest.mark.parametrize("key", ["implement", ".implement", "complex.", "a.b.c", "compelx.implement"])
def test_bad_rule_key_is_rejected(key: str) -> None:
    with pytest.raises(ValidationError, match="complexity"):
        RoutingSettings(rules={key: RouteOverride(model="x")})


def test_unknown_rule_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RoutingSettings.model_validate({"rules": {"*.review": {"modle": "opus"}}})
