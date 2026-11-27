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


def _task(task_id: str) -> dict[str, object]:
    return {"project": "fact", "task_key": f"gh:Kahncode/fact#{task_id}", "task_id": task_id}


WINDOW = {
    "name": "five_hour",
    "utilization": 0.93,
    "reserve": 0.1,
    "resets_at": "2026-11-26T18:00:00Z",
    "full": True,
}
PREVIEW = {
    "tasks": [
        {"task": _task("12"), "hold": "usage", "detail": "five_hour 93% (reserve 10%) resets 2026-11-26 18:00 UTC"},
        {"task": _task("13"), "hold": "failure_cap", "detail": "3 failed runs in a row: start it with `fact run`"},
        {"task": _task("14"), "hold": "dependency", "detail": "waits on gh:Kahncode/fact#12"},
    ],
    "usage": {"state": "full", "windows": [WINDOW], "reason": "five_hour 93%", "next_call_at": "2026-11-26T18:01:30Z"},
    "active": 1,
    "max_runs": 2,
}


def _use(monkeypatch: pytest.MonkeyPatch, settings: Settings, status: int = 200) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:  # noqa: PLR2004
            return httpx.Response(status, json={"detail": "No Temporal schedule fact-scheduler"})
        if request.url.path.endswith("/preview"):
            return httpx.Response(200, json=PREVIEW)
        paused = request.url.path.endswith("/off")
        return httpx.Response(200, json={**STATE, "paused": paused, "note": "paused" if paused else None})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(projects, "make_client", lambda: httpx.Client(base_url=settings.api_url, transport=transport))


@pytest.mark.parametrize(
    ("command", "shown"),
    [("on", "fact-scheduler: on"), ("off", "fact-scheduler: off (paused)"), ("status", "30 s")],
)
def test_scheduler_commands(settings: Settings, monkeypatch: pytest.MonkeyPatch, command: str, shown: str) -> None:
    _use(monkeypatch, settings)  # `on` and `off` only show the state the API answers with: off is the paused one
    result = CliRunner().invoke(app, ["scheduler", command])
    assert result.exit_code == 0, result.output
    assert shown in result.output


def test_status_previews_the_next_tick(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, settings)
    result = CliRunner().invoke(app, ["scheduler", "status"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "1 active of max 2" in out
    usage_line = next(line for line in out.splitlines() if "five_hour" in line)
    assert all(shown in usage_line for shown in ("93%", "10%", "18:00", "FULL"))
    assert "18:01" in out  # next usage check
    lines = [line for line in out.splitlines() if "gh:Kahncode/fact#" in line and "waits" in line]
    assert ["#12" in lines[0], "#13" in lines[1], "#14" in lines[2]] == [True, True, True]  # start order
    assert "failure_cap" in lines[1]


def test_missing_schedule_is_an_error(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, settings, status=404)
    result = CliRunner().invoke(app, ["scheduler", "status"])
    assert result.exit_code == 1
    assert "404" in result.output
