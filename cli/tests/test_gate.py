"""`fact gate`, `fact steer` and `fact stop` against a mocked API; the real handlers are tested in api/tests."""

import json

import httpx
import pytest
from fact.cli import projects
from fact.cli.main import app
from fact.core.settings import Settings
from typer.testing import CliRunner

LIVE = {"stage": "checks", "attempt": 1, "gate": None, "outcomes": []}
OPEN_GATE = {
    "gate_id": "g-1",
    "kind": "needs_human",
    "stage_id": "checks",
    "attempt": 1,
    "opened_at": "2026-01-01T00:00:00Z",
}


def _api(monkeypatch: pytest.MonkeyPatch, settings: Settings, *, gate: dict | None) -> list[tuple[str, str, object]]:
    """Fake a run `abc123` whose live status shows `gate`; return the POSTs received as (method, path, body)."""
    posts: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST":
            posts.append((request.method, path, json.loads(request.content)))
            return httpx.Response(200, json={})
        if path == "/api/v1/runs":  # lookup by workflow id
            return httpx.Response(200, json=[])
        if path.endswith("/status"):
            return httpx.Response(200, json={**LIVE, "gate": gate})
        return httpx.Response(200, json={"id": "abc123"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(projects, "make_client", lambda: httpx.Client(base_url=settings.api_url, transport=transport))
    return posts


def test_gate_answers_the_open_gate(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    posts = _api(monkeypatch, settings, gate=OPEN_GATE)

    result = CliRunner().invoke(app, ["gate", "abc123", "reject", "--note", "not yet"])

    assert result.exit_code == 0, result.output
    assert posts == [("POST", "/api/v1/runs/abc123/gates/g-1", {"decision": "reject", "note": "not yet"})]


def test_gate_with_an_explicit_id_skips_the_status_query(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    posts = _api(monkeypatch, settings, gate=None)

    result = CliRunner().invoke(app, ["gate", "abc123", "approve", "--gate", "g-7"])

    assert result.exit_code == 0, result.output
    assert [path for _, path, _ in posts] == ["/api/v1/runs/abc123/gates/g-7"]


def test_gate_without_an_open_gate_fails_and_answers_nothing(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    posts = _api(monkeypatch, settings, gate=None)

    result = CliRunner().invoke(app, ["gate", "abc123", "approve"])

    assert result.exit_code == 1
    assert "no open gate" in result.output
    assert posts == []


def test_steer_sends_the_text(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    posts = _api(monkeypatch, settings, gate=None)

    result = CliRunner().invoke(app, ["steer", "abc123", "use the fixture"])

    assert result.exit_code == 0, result.output
    assert posts == [("POST", "/api/v1/runs/abc123/instructions", {"text": "use the fixture"})]


def test_stop_sends_the_reason(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    posts = _api(monkeypatch, settings, gate=None)

    result = CliRunner().invoke(app, ["stop", "abc123", "--reason", "newer workflow"])

    assert result.exit_code == 0, result.output
    assert posts == [("POST", "/api/v1/runs/abc123/stop", {"reason": "newer workflow", "force": False})]
    assert "handoff note" in result.output


def test_stop_force_prints_the_workspace_to_delete(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    posts = _api(monkeypatch, settings, gate=None)

    result = CliRunner().invoke(app, ["stop", "abc123", "--force"])

    assert result.exit_code == 0, result.output
    assert posts == [("POST", "/api/v1/runs/abc123/stop", {"reason": "", "force": True})]
    assert str(settings.workspaces_dir / "abc123") in result.output


def test_stop_of_an_unknown_workflow_id_fails(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    posts = _api(monkeypatch, settings, gate=None)

    result = CliRunner().invoke(app, ["stop", "run-nope-1"])

    assert result.exit_code == 1
    assert "run-nope-1" in result.output
    assert posts == []
