"""A project's `step_defaults` overrides the SDK's step limits value by value."""

from pathlib import Path

from fact.core.project_config import load_project_config
from fact.sdk.step_defaults import SDK_STEP_DEFAULTS, resolve

PROJECT = """name: acme
repositories:
  - full_name: acme/home
    role: backend
workflows:
  default: standard
"""


def _config(tmp_path: Path, extra: str = "") -> Path:
    (tmp_path / ".fact").mkdir()
    (tmp_path / ".fact" / "project.yaml").write_text(PROJECT + extra, encoding="utf-8")
    return tmp_path


def test_one_override_keeps_the_other_sdk_values(tmp_path: Path) -> None:
    config = load_project_config(_config(tmp_path, "step_defaults:\n  agent:\n    max_turns: 7\n"))
    limits = resolve(config.step_defaults)
    assert limits.agent.max_turns == 7  # noqa: PLR2004
    sdk = SDK_STEP_DEFAULTS
    assert (limits.agent.timeout_minutes, limits.agent.permission_mode) == (
        sdk.agent.timeout_minutes,
        sdk.agent.permission_mode,
    )
    assert limits.shell == sdk.shell


def test_a_project_without_step_defaults_runs_on_the_sdk_values(tmp_path: Path) -> None:
    assert resolve(load_project_config(_config(tmp_path)).step_defaults) == SDK_STEP_DEFAULTS
