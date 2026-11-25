"""`step_defaults`: setting one value in YAML keeps the other defaults."""

from pathlib import Path

import pytest
from fact.core.enums import StepKind
from fact.core.settings import CONFIG_VAR, Settings, StepDefaults, reset_settings

AGENT_TIMEOUT = 90.0
DEFAULT_MAX_TURNS = 100
SHELL_TIMEOUT = 15.0


def test_partial_yaml_keeps_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "fact.yaml"
    config.write_text("step_defaults:\n  agent:\n    timeout_minutes: 90\n", encoding="utf-8")
    monkeypatch.setenv(CONFIG_VAR, str(config))
    reset_settings()
    try:
        settings = Settings()
    finally:
        reset_settings()
    agent = settings.step_default(StepKind.AGENT)
    assert agent.timeout_minutes == AGENT_TIMEOUT
    assert agent.max_turns == DEFAULT_MAX_TURNS
    assert settings.step_default(StepKind.SHELL).timeout_minutes == SHELL_TIMEOUT


def test_kind_without_field_gets_base_defaults() -> None:
    assert Settings().step_default(StepKind.GATE) == StepDefaults()
