"""`fact run`, `fact runs` and `fact status` against a mocked API."""

import json
from collections.abc import Callable

import httpx
import pytest
from fact.cli import projects
from fact.cli.main import app
from fact.core.settings import Settings
from typer.testing import CliRunner

BASE = "http://127.0.0.1:8100"
RUN = {
    "id": "abc123",
    "workflow_id": "run-task-M1-02-1",
    "project_id": 1,
    "task_key": "task:M1-02",
    "workflow_type": "standard",
    "status": "finished",
    "outcome": "pr_opened",
    "complexity": "standard",
    "worker_version": None,
    "home_sha": None,
    "created_at": "2026-01-01T00:00:00Z",
    "started_at": "2026-01-01T00:00:00Z",
    "ended_at": "2026-01-01T00:02:05Z",
    "machine_seconds": 120.0,
    "human_wait_seconds": 0.0,
    "tokens_in": 100,
    "tokens_out": 10,
    "cache_read_tokens": 60,
    "cache_write_tokens": 30,
    "cost_usd": "1.234",
    "cost_unknown_steps": 0,
    "human_touches": 0,
}
STEP = {
    "id": "s1",
    "run_id": "abc123",
    "stage_id": "implement",
    "kind": "agent",
    "attempt": 1,
    "status": "finished",
    "outcome": "passed",
    "harness": "claude-code",
    "model": "sonnet",
    "queued_at": None,
    "started_at": "2026-01-01T00:00:00Z",
    "ended_at": "2026-01-01T00:01:05Z",
    "wait_seconds": 0.0,
    "tokens_in": 100,
    "tokens_out": 10,
    "cache_read_tokens": 60,
    "cache_write_tokens": 30,
    "cost_usd": "1.234",
    "usage_partial": False,
    "log_ref": "runs/abc123/implement/log.txt",
    "transcript_ref": None,
    "outputs": None,
}

Handler = Callable[[httpx.Request], httpx.Response]


def _use(monkeypatch: pytest.MonkeyPatch, settings: Settings, handler: Handler) -> None:
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(projects, "make_client", lambda: httpx.Client(base_url=settings.api_url, transport=transport))


def test_run_starts(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{BASE}/api/v1/runs/start"
        bodies.append(json.loads(request.content))
        started = {
            "run_id": "abc123",
            "workflow_id": "run-task-M1-02-1",
            "workflow_type": "standard",
            "project": "fact",
            "temporal_url": "http://localhost:8233/namespaces/default/workflows/run-task-M1-02-1",
        }
        return httpx.Response(201, json=started)

    _use(monkeypatch, settings, handler)
    result = CliRunner().invoke(app, ["run", "task:M1-02", "--force-deps"])

    assert result.exit_code == 0, result.output
    assert bodies == [{"task_key": "task:M1-02", "project": None, "override_deps": True}]
    assert "Started run-task-M1-02-1" in result.output
    assert "http://localhost:8233/namespaces/default/workflows/run-task-M1-02-1" in result.output


def test_run_refused_for_dependencies(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    detail = {"message": "x", "missing_deps": ["task:M1-01"]}
    _use(monkeypatch, settings, lambda _: httpx.Response(409, json={"detail": detail}))
    result = CliRunner().invoke(app, ["run", "task:M1-02"])
    assert result.exit_code == 1
    assert "unfinished dependencies: task:M1-01" in result.output
    assert "--force-deps" in result.output


def test_run_api_down(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        msg = "refused"
        raise httpx.ConnectError(msg, request=request)

    _use(monkeypatch, settings, handler)
    result = CliRunner().invoke(app, ["run", "task:M1-02"])
    assert result.exit_code == 1
    assert "Is `fact dev` running?" in result.output


def test_runs_lists_with_filters(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=[RUN])

    _use(monkeypatch, settings, handler)
    result = CliRunner().invoke(app, ["runs", "--project", "fact", "--status", "finished"])

    assert result.exit_code == 0, result.output
    assert seen == [f"{BASE}/api/v1/runs?project=fact&status=finished"]
    lines = result.output.splitlines()
    assert lines[0].split() == ["WORKFLOW", "STATUS", "OUTCOME", "COST", "CREATED", "RUN"]
    assert lines[1].split() == ["run-task-M1-02-1", "finished", "pr_opened", "$1.23", "2026-01-01T00:00:00", "abc123"]


def test_runs_empty(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, settings, lambda _: httpx.Response(200, json=[]))
    result = CliRunner().invoke(app, ["runs"])
    assert result.exit_code == 0
    assert result.output.strip() == "No runs."


def test_status_by_workflow_id(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/api/v1/runs":
            return httpx.Response(200, json=[RUN])
        return httpx.Response(200, json={**RUN, "steps": [STEP]})

    _use(monkeypatch, settings, handler)
    result = CliRunner().invoke(app, ["status", "run-task-M1-02-1"])

    assert result.exit_code == 0, result.output
    assert seen == [f"{BASE}/api/v1/runs?workflow_id=run-task-M1-02-1", f"{BASE}/api/v1/runs/abc123"]
    assert "outcome: pr_opened" in result.output
    assert "Duration: 2m05s" in result.output
    assert "Cost:     $1.23" in result.output
    assert "Tokens:   in 10, cache read 60, cache write 30, out 10" in result.output
    step_line = next(line for line in result.output.splitlines() if line.startswith("implement"))
    assert step_line.split()[:7] == ["implement", "1", "agent", "finished", "passed", "1m05s", "$1.23"]
    assert f"log: {BASE}/api/v1/artifacts/runs/abc123/implement/log.txt" in step_line


def test_status_unknown_run(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, settings, lambda _: httpx.Response(404, json={"detail": "Unknown run: nope"}))
    result = CliRunner().invoke(app, ["status", "nope"])
    assert result.exit_code == 1
    assert "API answered 404: Unknown run: nope" in result.output


def test_status_shows_a_failed_live_query(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/status"):
            return httpx.Response(502, json={"detail": "Status query failed"})
        return httpx.Response(200, json={**RUN, "status": "running", "outcome": None, "steps": []})

    _use(monkeypatch, settings, handler)
    result = CliRunner().invoke(app, ["status", "abc123"])
    assert result.exit_code == 0, result.output
    assert "Gate:     unknown (Status query failed)" in result.output


def test_status_shows_why_the_run_waits(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/status"):
            live = {"stage": "implement", "attempt": 1, "gate": None, "outcomes": []}
            return httpx.Response(200, json={**live, "waiting": "usage limit until 2026-11-26 15:00 UTC"})
        return httpx.Response(200, json={**RUN, "status": "waiting", "outcome": None, "steps": []})

    _use(monkeypatch, settings, handler)
    result = CliRunner().invoke(app, ["status", "abc123"])
    assert result.exit_code == 0, result.output
    assert "Status:   waiting" in result.output
    assert "Gate:     none open" in result.output
    assert "Waiting:  usage limit until 2026-11-26 15:00 UTC" in result.output


def test_steer_refused_for_a_finished_run(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/instructions"):
            assert json.loads(request.content) == {"text": "more tests"}
            return httpx.Response(409, json={"detail": "Run run-task-M1-02-1 is not running in Temporal"})
        return httpx.Response(200, json={**RUN, "steps": []})

    _use(monkeypatch, settings, handler)
    result = CliRunner().invoke(app, ["steer", "abc123", "more tests"])
    assert result.exit_code == 1
    assert "API answered 409: Run run-task-M1-02-1 is not running in Temporal" in result.output


def test_status_shows_an_unreported_cost_as_unknown(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    run = {**RUN, "cost_unknown_steps": 1}
    step = {**STEP, "cost_usd": None, "usage_partial": True}
    _use(monkeypatch, settings, lambda _: httpx.Response(200, json={**run, "steps": [step]}))
    result = CliRunner().invoke(app, ["status", "abc123"])
    assert result.exit_code == 0, result.output
    assert "Cost:     $1.23 (+ 1 agent steps with no reported cost)" in result.output
    step_line = next(line for line in result.output.splitlines() if line.startswith("implement"))
    assert "unknown (cut short)" in step_line
