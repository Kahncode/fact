"""The environment child processes get: the worker's own, plus what the caller passes."""

import pytest
from fact.worker.runners.process import build_env


def test_build_env_inherits_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATHEXT", ".COM;.EXE")  # pre-commit on Windows finds git.exe through it
    env = build_env({"EXTRA": "1"})
    assert env["PATHEXT"] == ".COM;.EXE"
    assert env["EXTRA"] == "1"


def test_build_env_lets_the_caller_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/worker")
    assert build_env({"HOME": "/home/agent"})["HOME"] == "/home/agent"
