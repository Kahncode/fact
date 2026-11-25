"""Shared fixtures for the CLI tests."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fact.core.settings import CONFIG_VAR, Settings, get_settings, reset_settings


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    config = tmp_path / "fact.yaml"
    config.write_text(
        f"data_dir: {(tmp_path / 'data').as_posix()}\ntemporal:\n  address: localhost:7300\napi:\n  port: 8100\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(CONFIG_VAR, str(config))
    reset_settings()
    yield get_settings()
    reset_settings()
