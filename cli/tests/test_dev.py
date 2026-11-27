import os
from pathlib import Path

import pytest
import typer
from fact.cli import dev
from fact.cli.main import app
from fact.cli.shutdown import ShutdownWatch
from fact.core.settings import Settings
from typer.testing import CliRunner


def test_build_commands(settings: Settings, tmp_path: Path) -> None:
    temporal, api, worker = dev.build_commands(settings, "/bin/temporal", environ={})

    assert temporal.argv[0] == "/bin/temporal"
    assert temporal.argv[temporal.argv.index("--db-filename") + 1] == str(tmp_path / "data" / "temporal.db")
    assert temporal.argv[temporal.argv.index("--port") + 1] == "7300"  # from settings.temporal.address
    assert api.argv[-2:] == ["-m", "fact.api"]
    assert worker.argv[-2:] == ["-m", "fact.worker"]
    assert worker.wait_for == ("localhost", 7300)  # the worker starts once Temporal listens


def test_dev_fails_without_temporal(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG001
    monkeypatch.setattr(dev.shutil, "which", lambda _: None)
    called = []
    monkeypatch.setattr(dev, "run_all", called.append)

    result = CliRunner().invoke(app, ["dev"])

    assert result.exit_code == 1
    assert "`temporal` is not on PATH" in result.output
    assert called == []


@pytest.mark.parametrize(("supervisor_code", "exit_code"), [(130, 0), (3, 3)])  # 130: Ctrl-C is a clean stop
def test_dev_creates_data_dir_and_exits_with_the_supervisor_code(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, supervisor_code: int, exit_code: int
) -> None:
    monkeypatch.setattr(dev.shutil, "which", lambda _: "temporal")
    monkeypatch.setattr(dev, "run_all", lambda _specs, _restarter: supervisor_code)

    result = CliRunner().invoke(app, ["dev"])

    assert result.exit_code == exit_code
    assert settings.data_dir.is_dir()


@pytest.mark.parametrize(
    ("address", "endpoint"),
    [("localhost:7233", ("localhost", 7233)), ("[::1]:7300", ("::1", 7300)), (":7233", ("localhost", 7233))],
)
def test_temporal_endpoint(address: str, endpoint: tuple[str, int]) -> None:
    assert dev.temporal_endpoint(Settings(temporal={"address": address})) == endpoint


def test_temporal_endpoint_rejects_missing_port() -> None:
    with pytest.raises(typer.BadParameter, match="host:port"):
        dev.temporal_endpoint(Settings(temporal={"address": "localhost"}))


def test_build_commands_from_pinned(settings: Settings, tmp_path: Path) -> None:
    checkout = tmp_path / "pinned" / "abc"
    temporal, api, worker = dev.build_commands(settings, "temporal", checkout, environ={})

    python = str(dev.venv_python(checkout))
    assert api.argv == [python, "-m", "fact.api"]
    assert worker.argv == [python, "-m", "fact.worker"]
    assert api.env == worker.env == {}  # the checkout reads its own fact.yaml and .env
    assert temporal.env == {}


def test_dev_pinned_without_pin_fails(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG001
    monkeypatch.setattr(dev.shutil, "which", lambda name: name)
    called = []
    monkeypatch.setattr(dev, "run_all", called.append)

    result = CliRunner().invoke(app, ["dev", "--pinned"])

    assert result.exit_code == 1
    assert "fact use" in result.output
    assert called == []


def test_dev_pinned_runs_current_pin(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    sha = "c" * 40
    checkout = settings.pinned_dir / "factory" / sha
    checkout.mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("", encoding="utf-8")
    dev.venv_python(checkout).parent.mkdir(parents=True)
    dev.venv_python(checkout).write_text("", encoding="utf-8")
    (settings.pinned_dir / "current").write_text(f"{sha}\n", encoding="utf-8")
    monkeypatch.setattr(dev.shutil, "which", lambda name: name)
    started: list[list[str]] = []

    def fake_run_all(specs: list, restarter: object) -> int:
        assert isinstance(restarter, ShutdownWatch)  # plain and pinned runs exit on `fact shutdown` too
        started.extend(s.argv for s in specs)
        return 130

    monkeypatch.setattr(dev, "run_all", fake_run_all)

    result = CliRunner().invoke(app, ["dev", "--pinned"])

    assert result.exit_code == 0, result.output
    assert f"Running pinned {sha}" in result.output
    assert started[1][0] == str(dev.venv_python(checkout))


def test_children_start_without_the_venv_activation(settings: Settings, tmp_path: Path) -> None:
    venv = tmp_path / "factory" / ".venv"
    scripts = venv / ("Scripts" if dev.IS_WINDOWS else "bin")
    other = tmp_path / "tools"
    environ = {"VIRTUAL_ENV": str(venv), "PATH": os.pathsep.join([str(scripts), str(other)])}

    temporal, api, worker = dev.build_commands(settings, "temporal", environ=environ)

    assert api.env["PATH"] == worker.env["PATH"] == str(other)
    assert api.unset == worker.unset == dev.VENV_VARS
    assert temporal.unset == ()


def test_path_is_kept_without_an_active_venv() -> None:
    assert dev.deactivated_path({"PATH": "/bin"}) is None


def test_dev_pinned_without_venv_fails(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    sha = "d" * 40
    (settings.pinned_dir / "factory" / sha).mkdir(parents=True)
    (settings.pinned_dir / "factory" / sha / "pyproject.toml").write_text("", encoding="utf-8")
    (settings.pinned_dir / "current").write_text(f"{sha}\n", encoding="utf-8")
    monkeypatch.setattr(dev.shutil, "which", lambda name: name)
    monkeypatch.setattr(dev, "run_all", lambda _specs, _restarter: 0)

    result = CliRunner().invoke(app, ["dev", "--pinned"])

    assert result.exit_code == 1
    assert "run `fact use` again" in result.output


def test_dev_follow_pins_when_nothing_is_pinned_and_supervises_with_a_follower(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha = "e" * 40
    checkout = settings.pinned_dir / "factory" / sha

    def fake_pin(_settings: Settings, ref: str, _uv: str) -> Path:
        assert ref == "main"
        dev.venv_python(checkout).parent.mkdir(parents=True)
        dev.venv_python(checkout).write_text("", encoding="utf-8")
        (checkout / "pyproject.toml").write_text("", encoding="utf-8")
        (settings.pinned_dir / "current").write_text(f"{sha}\n", encoding="utf-8")
        return checkout

    monkeypatch.setattr(dev.shutil, "which", lambda name: name)
    monkeypatch.setattr(dev, "pin", fake_pin)
    supervised = []
    monkeypatch.setattr(dev, "run_all", lambda _specs, restarter: supervised.append(restarter) or 130)

    result = CliRunner().invoke(app, ["dev", "--follow", "main"])

    assert result.exit_code == 0, result.output
    assert supervised[0].pinned == sha
