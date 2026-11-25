import sys
from pathlib import Path

import pytest
import typer
from fact.cli import dev
from fact.cli.main import app
from fact.core.settings import CONFIG_VAR, Settings, config_file
from typer.testing import CliRunner


def test_build_commands(settings: Settings) -> None:
    temporal, api, worker = dev.build_commands(settings, "/bin/temporal")

    assert temporal.name == "temporal"
    assert temporal.argv == [
        "/bin/temporal",
        "server",
        "start-dev",
        "--db-filename",
        str(settings.temporal_db_path),
        "--port",
        "7300",
        "--ui-port",
        "8233",
    ]
    assert temporal.wait_for is None
    assert api.argv == [sys.executable, "-m", "fact.api"]
    assert api.wait_for is None
    assert worker.argv == [sys.executable, "-m", "fact.worker"]
    assert worker.wait_for == ("localhost", 7300)


def test_temporal_db_lives_in_data_dir(settings: Settings, tmp_path: Path) -> None:
    temporal, *_ = dev.build_commands(settings, "temporal")
    assert Path(temporal.argv[4]) == tmp_path / "data" / "temporal.db"


def test_dev_fails_without_temporal(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG001
    monkeypatch.setattr(dev.shutil, "which", lambda _: None)
    called = []
    monkeypatch.setattr(dev, "run_all", called.append)

    result = CliRunner().invoke(app, ["dev"])

    assert result.exit_code == 1
    assert "`temporal` is not on PATH" in result.output
    assert called == []


def test_dev_creates_data_dir_and_runs(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dev.shutil, "which", lambda _: "temporal")
    started: list[list[str]] = []

    def fake_run_all(specs: list) -> int:
        started.extend(s.name for s in specs)
        return 130  # Ctrl-C

    monkeypatch.setattr(dev, "run_all", fake_run_all)

    result = CliRunner().invoke(app, ["dev"])

    assert result.exit_code == 0
    assert settings.data_dir.is_dir()
    assert started == ["temporal", "api", "worker"]


def test_version() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.startswith("fact ")


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
    temporal, api, worker = dev.build_commands(settings, "temporal", checkout, "/bin/uv")

    launcher = ["/bin/uv", "run", "--frozen", "--project", str(checkout), "python"]
    assert api.argv == [*launcher, "-m", "fact.api"]
    assert worker.argv == [*launcher, "-m", "fact.worker"]
    assert api.env == worker.env == {CONFIG_VAR: str(config_file().resolve())}
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
    (settings.pinned_dir / "current").write_text(f"{sha}\n", encoding="utf-8")
    monkeypatch.setattr(dev.shutil, "which", lambda name: name)
    started: list[list[str]] = []

    def fake_run_all(specs: list) -> int:
        started.extend(s.argv for s in specs)
        return 130

    monkeypatch.setattr(dev, "run_all", fake_run_all)

    result = CliRunner().invoke(app, ["dev", "--pinned"])

    assert result.exit_code == 0, result.output
    assert f"Running pinned {sha}" in result.output
    assert started[1][:5] == ["uv", "run", "--frozen", "--project", str(checkout)]
