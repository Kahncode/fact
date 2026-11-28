"""`fact reset`: every piece of factory data under `data_dir` goes; it refuses while `fact dev` runs."""

import subprocess
from pathlib import Path

import pytest
from fact.cli import reset
from fact.cli.dev_lock import LOCK_NAME, DevLock
from fact.cli.main import app
from fact.core.settings import Settings
from typer.testing import CliRunner

FACTORY_DATA = [
    "fact.db",
    "fact.db-wal",
    "temporal.db",
    "artifacts/runs/r1/handoff.md",
    "step-logs/r1/implement/1/log.txt",
    "workspaces/r1/README.md",
    "repos/fact/HEAD",
    "pinned/current",
    "run/api.json",
]


@pytest.fixture
def source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, capture_output=True, check=True)  # noqa: S607
    (repo / ".env").write_text("SECRET=1\n", encoding="utf-8")
    monkeypatch.setattr(reset, "REPO_ROOT", repo)
    return repo


@pytest.fixture
def data(settings: Settings) -> Path:
    for name in [*FACTORY_DATA, "notes.txt"]:
        path = settings.data_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    return settings.data_dir


def _left(data: Path) -> set[str]:
    return {p.relative_to(data).as_posix() for p in data.rglob("*")}


def test_reset_yes_leaves_no_factory_data(data: Path, source: Path) -> None:
    result = CliRunner().invoke(app, ["reset", "--yes"])

    assert result.exit_code == 0, result.output
    assert _left(data) == {"notes.txt", LOCK_NAME}
    assert (source / ".env").is_file()
    assert "fact:needs-human" in result.output


def test_reset_dry_run_changes_nothing(data: Path, source: Path) -> None:  # noqa: ARG001
    before = _left(data)

    result = CliRunner().invoke(app, ["reset", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert _left(data) == before


def test_reset_declined_changes_nothing_but_the_lock(data: Path, source: Path) -> None:  # noqa: ARG001
    before = _left(data)

    CliRunner().invoke(app, ["reset"], input="n\n")

    assert _left(data) == before | {LOCK_NAME}


def test_reset_dry_run_refuses_while_fact_dev_runs(data: Path, source: Path) -> None:  # noqa: ARG001
    lock = DevLock(data / LOCK_NAME)
    lock.acquire()
    try:
        result = CliRunner().invoke(app, ["reset", "--dry-run"])
    finally:
        lock.release()

    assert result.exit_code == 1
    assert "already running" in result.output


def test_reset_refuses_while_fact_dev_runs(data: Path, source: Path) -> None:  # noqa: ARG001
    lock = DevLock(data / LOCK_NAME)
    lock.acquire()
    try:
        result = CliRunner().invoke(app, ["reset", "--yes"])
    finally:
        lock.release()

    assert result.exit_code == 1
    assert "already running" in result.output
    assert (data / "fact.db").is_file()
