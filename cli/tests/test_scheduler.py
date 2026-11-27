"""`fact scheduler on|off|status` against a mocked API."""

import httpx
import pytest
from fact.cli import projects
from fact.cli.main import app
from fact.core.settings import Settings
from typer.testing import CliRunner

STATE = {
    "in_process": True,
    "on": True,
    "interval_seconds": 30.0,
    "last_tick_at": "2026-11-26T10:00:00Z",
    "last_tick": {"started": ["run-task-M1-01-1"], "failed": [], "usage_full": "", "skipped": False},
}


def _task(task_id: str, priority: str = "medium", effective: str = "medium") -> dict[str, object]:
    key = f"gh:Kahncode/fact#{task_id}"
    return {
        "project": "fact",
        "task_key": key,
        "task_id": task_id,
        "priority": priority,
        "effective_priority": effective,
    }


WINDOW = {
    "name": "five_hour",
    "utilization": 0.93,
    "reserve": 0.1,
    "resets_at": "2026-11-26T18:00:00Z",
    "full": True,
}
PREVIEW = {
    "tasks": [
        {
            "task": _task("12", "low", "high"),
            "hold": "usage",
            "detail": "five_hour 93% (reserve 10%) resets 2026-11-26 18:00 UTC",
        },
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
            return httpx.Response(status, json={"detail": "Not Found"})
        if request.url.path.endswith("/preview"):
            return httpx.Response(200, json=PREVIEW)
        return httpx.Response(200, json={**STATE, "on": not request.url.path.endswith("/off")})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(projects, "make_client", lambda: httpx.Client(base_url=settings.api_url, transport=transport))


@pytest.mark.parametrize(
    ("command", "shown"),
    [("on", "Scheduler: on"), ("off", "Scheduler: off"), ("status", "1 started")],
)
def test_scheduler_commands(settings: Settings, monkeypatch: pytest.MonkeyPatch, command: str, shown: str) -> None:
    _use(monkeypatch, settings)  # `on` and `off` only show the state the API answers with
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
    assert "low (eff. high)" in lines[0]
    assert "medium" in lines[1]
    assert "eff." not in lines[1]


def test_no_switch_outside_dev_is_an_error(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, settings, status=404)
    result = CliRunner().invoke(app, ["scheduler", "off"])
    assert result.exit_code == 1
    assert "404" in result.output


def test_shutdown_turns_the_scheduler_off_and_shows_the_active_runs(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/active"):
            return httpx.Response(200, json={"active": 2})
        return httpx.Response(200, json={**STATE, "on": False, "shutdown": True})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(projects, "make_client", lambda: httpx.Client(base_url=settings.api_url, transport=transport))
    result = CliRunner().invoke(app, ["shutdown"])

    assert result.exit_code == 0, result.output
    assert ("POST", "/api/v1/scheduler/shutdown") in calls
    assert "2 run(s) active: `fact dev` exits once they end." in result.output
