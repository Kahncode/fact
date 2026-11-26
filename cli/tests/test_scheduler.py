"""`fact scheduler on|off|status` against a mocked API."""

import httpx
import pytest
from fact.cli import projects
from fact.cli.main import app
from fact.core.settings import Settings
from typer.testing import CliRunner

STATE = {
    "schedule_id": "fact-scheduler",
    "paused": False,
    "note": None,
    "interval_seconds": 30.0,
    "next_ticks": ["2026-11-26T10:00:30Z"],
    "recent_ticks": ["2026-11-26T10:00:00Z"],
}


def _use(monkeypatch: pytest.MonkeyPatch, settings: Settings, calls: list[str], status: int = 200) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if status != 200:  # noqa: PLR2004
            return httpx.Response(status, json={"detail": "No Temporal schedule fact-scheduler"})
        paused = request.url.path.endswith("/off")
        return httpx.Response(200, json={**STATE, "paused": paused, "note": "paused" if paused else None})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(projects, "make_client", lambda: httpx.Client(base_url=settings.api_url, transport=transport))


@pytest.mark.parametrize(
    ("command", "call", "shown"),
    [
        ("on", "POST /api/v1/scheduler/on", "fact-scheduler: on"),
        ("off", "POST /api/v1/scheduler/off", "fact-scheduler: off (paused)"),
        ("status", "GET /api/v1/scheduler", "Every:     30 s"),
    ],
)
def test_scheduler_commands(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, command: str, call: str, shown: str
) -> None:
    calls: list[str] = []
    _use(monkeypatch, settings, calls)
    result = CliRunner().invoke(app, ["scheduler", command])
    assert result.exit_code == 0, result.output
    assert calls == [call]
    assert shown in result.output


def test_missing_schedule_is_an_error(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, settings, [], status=404)
    result = CliRunner().invoke(app, ["scheduler", "status"])
    assert result.exit_code == 1
    assert "404" in result.output
