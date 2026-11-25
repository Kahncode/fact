import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest
import structlog
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fact.cli import github
from fact.cli.main import app
from fact.core.github import FACT_LABELS, GitHubApp
from fact.core.settings import Settings, reset_settings
from typer.testing import CliRunner

TOKEN = "ghs_cli_secret"  # noqa: S105 fake


def _pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def _handler(request: httpx.Request) -> httpx.Response:
    match request.url.path:
        case "/app":
            return httpx.Response(200, json={"id": 12, "slug": "fact-factory", "name": "fact"})
        case "/repos/Kahncode/fact/installation":
            return httpx.Response(200, json={"id": 34, "account": {"login": "Kahncode"}})
        case "/app/installations/34/access_tokens":
            return httpx.Response(201, json={"token": TOKEN, "expires_at": "2026-11-25T13:00:00Z"})
    return httpx.Response(404, json={"message": "Not Found"})


@pytest.fixture(autouse=True)
def _isolate(settings: Settings) -> Iterator[None]:  # noqa: ARG001 settings: tmp fact.yaml
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:], root.level = handlers, level
    structlog.reset_defaults()


@pytest.fixture
def fake_app(monkeypatch: pytest.MonkeyPatch) -> None:
    gh = GitHubApp(
        12,
        _pem(),
        api_url="https://gh.test",
        transport=httpx.MockTransport(_handler),
        clock=lambda: datetime(2026, 11, 25, 12, tzinfo=UTC),
    )
    monkeypatch.setattr(github, "make_app", lambda: gh)


@pytest.mark.usefixtures("fake_app")
def test_check_prints_app_installation_and_expiry() -> None:
    result = CliRunner().invoke(app, ["github", "check", "Kahncode/fact"])

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "app:          fact-factory (id 12)",
        "installation: 34 (account Kahncode)",
        "token:        expires 2026-11-25T13:00:00+00:00",
    ]
    assert TOKEN not in result.output


@pytest.mark.usefixtures("fake_app")
def test_check_not_installed() -> None:
    result = CliRunner().invoke(app, ["github", "check", "someone/else"])

    assert result.exit_code == 1
    assert "404" in result.output
    assert "installed" in result.output


def test_check_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FACT_GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("FACT_GITHUB_APP_PRIVATE_KEY_PATH", raising=False)
    reset_settings()

    result = CliRunner().invoke(app, ["github", "check", "Kahncode/fact"])

    assert result.exit_code == 1
    assert "FACT_GITHUB_APP_ID" in result.output


def test_check_unexpected_response(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app":
            return httpx.Response(200, json={"unexpected": True})
        return _handler(request)

    gh = GitHubApp(12, _pem(), api_url="https://gh.test", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(github, "make_app", lambda: gh)

    result = CliRunner().invoke(app, ["github", "check", "Kahncode/fact"])

    assert result.exit_code == 1
    assert "unexpected GitHub response" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def _labels_handler(existing: list[dict], writes: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/Kahncode/fact/labels":
            if request.method == "GET":
                assert request.headers["Authorization"] == f"Bearer {TOKEN}"
                return httpx.Response(200, json=existing)
            writes.append(request.method)
            existing.append(json.loads(request.content))
            return httpx.Response(201, json=existing[-1])
        return _handler(request)

    return httpx.MockTransport(handler)


def test_labels_sync_creates_then_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    existing: list[dict] = []
    writes: list[str] = []
    gh = GitHubApp(12, _pem(), api_url="https://gh.test", transport=_labels_handler(existing, writes))
    monkeypatch.setattr(github, "make_app", lambda: gh)

    first = CliRunner().invoke(app, ["github", "labels", "sync", "Kahncode/fact"])
    second = CliRunner().invoke(app, ["github", "labels", "sync", "Kahncode/fact"])

    assert first.exit_code == 0, first.output
    assert "created    fact:ready" in first.output.splitlines()
    assert first.output.splitlines()[-1] == f"{len(FACT_LABELS)} of {len(FACT_LABELS)} labels changed on Kahncode/fact"
    assert second.exit_code == 0, second.output
    assert second.output.splitlines()[-1] == f"0 of {len(FACT_LABELS)} labels changed on Kahncode/fact"
    assert writes == ["POST"] * len(FACT_LABELS)
    assert TOKEN not in first.output + second.output


@pytest.mark.usefixtures("fake_app")
def test_labels_sync_not_installed() -> None:
    result = CliRunner().invoke(app, ["github", "labels", "sync", "someone/else"])

    assert result.exit_code == 1
    assert "404" in result.output
