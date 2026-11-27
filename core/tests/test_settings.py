# ruff: noqa: PLR2004
from collections.abc import Iterator
from pathlib import Path

import pytest
from fact.core.settings import (
    AppEnv,
    ProjectRef,
    Settings,
    get_settings,
    override_settings_class,
    reset_settings,
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for var in ("FACT_ENV", "FACT_TEMPORAL__ADDRESS", "FACT_DATA_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FACT_CONFIG", str(tmp_path / "fact.yaml"))
    reset_settings()
    yield
    reset_settings()


@pytest.mark.parametrize(
    ("host", "url"),
    [
        ("127.0.0.1", "http://127.0.0.1:9000"),
        ("0.0.0.0", "http://127.0.0.1:9000"),  # noqa: S104
        ("::", "http://[::1]:9000"),
        ("::1", "http://[::1]:9000"),
        ("[::1]", "http://[::1]:9000"),
    ],
)
def test_api_url_maps_wildcard_hosts_to_loopback(host: str, url: str) -> None:
    assert Settings(api={"host": host, "port": 9000}).api_url == url


def test_yaml_loading(tmp_path: Path) -> None:
    (tmp_path / "fact.yaml").write_text(
        "data_dir: ~/other\n"
        "temporal:\n  namespace: ns\n"
        "projects:\n  - name: fact\n    home: Kahncode/fact\n    path: .\n",
        encoding="utf-8",
    )
    s = Settings()
    assert s.data_dir == Path.home() / "other"
    assert s.temporal.namespace == "ns"
    assert s.temporal.address == "localhost:7233"  # untouched sibling keeps its default
    assert s.projects == [ProjectRef(name="fact", home="Kahncode/fact", path=Path())]


def test_env_overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "fact.yaml").write_text("api:\n  port: 1111\nconcurrency:\n  max_failed_runs: 5\n", encoding="utf-8")
    (tmp_path / "fact.dev.yaml").write_text("api:\n  port: 2222\n", encoding="utf-8")
    assert Settings().api.port == 1111  # no overlay for env local
    monkeypatch.setenv("FACT_ENV", "dev")
    s = Settings()
    assert s.env is AppEnv.DEV
    assert s.api.port == 2222
    assert s.concurrency.max_failed_runs == 5


def test_env_var_overrides_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "fact.yaml").write_text("temporal:\n  address: yaml:1\n", encoding="utf-8")
    monkeypatch.setenv("FACT_TEMPORAL__ADDRESS", "x:1")
    assert get_settings().temporal.address == "x:1"


def test_dotenv_next_to_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "fact.yaml").write_text("temporal:\n  address: yaml:1\n", encoding="utf-8")
    (tmp_path / ".env").write_text("FACT_TEMPORAL__ADDRESS=dotenv:2\nFACT_GITHUB_APP_ID=42\n", encoding="utf-8")
    assert Settings().temporal.address == "dotenv:2"
    monkeypatch.setenv("FACT_TEMPORAL__ADDRESS", "env:3")
    assert Settings().temporal.address == "env:3"


def test_dotenv_selects_overlay(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("FACT_ENV=test\n", encoding="utf-8")
    (tmp_path / "fact.test.yaml").write_text("api:\n  port: 3333\n", encoding="utf-8")
    assert Settings().api.port == 3333


def test_get_settings_is_cached_and_resettable(monkeypatch: pytest.MonkeyPatch) -> None:
    first = get_settings()
    assert get_settings() is first
    monkeypatch.setenv("FACT_TEMPORAL__ADDRESS", "y:2")
    assert get_settings().temporal.address == "localhost:7233"
    reset_settings()
    assert get_settings().temporal.address == "y:2"


def test_yaml_env_var_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("from-file\n", encoding="utf-8")
    monkeypatch.setenv("TEMPORAL_HOST", "t.example")
    (tmp_path / "fact.yaml").write_text(
        f"temporal:\n  address: ${{TEMPORAL_HOST}}:7233\n  namespace: ${{{secret.as_posix()}}}\n"
        "api:\n  host: ${UNSET_VAR_FOR_TEST}\n",
        encoding="utf-8",
    )
    s = Settings()
    assert s.temporal.address == "t.example:7233"
    assert s.temporal.namespace == "from-file"
    assert s.api.host == "${UNSET_VAR_FOR_TEST}"  # unresolved references stay as written


def test_override_settings_class_only_before_first_load() -> None:
    class AppSettings(Settings):
        extra_flag: bool = True

    override_settings_class(AppSettings)
    assert isinstance(get_settings(), AppSettings)
    with pytest.raises(RuntimeError):
        override_settings_class(AppSettings)


def test_override_after_load_fails() -> None:
    get_settings()
    with pytest.raises(RuntimeError):
        override_settings_class(Settings)
