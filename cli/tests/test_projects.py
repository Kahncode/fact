import httpx
import pytest
from fact.cli import projects
from fact.cli.main import app
from fact.core.settings import Settings
from typer.testing import CliRunner

PROJECTS = [
    {
        "id": 1,
        "name": "fact",
        "home_repo": "Kahncode/fact",
        "repositories": [
            {"full_name": "Kahncode/fact", "role": None, "default_branch": "main", "merge_order": 0},
            {"full_name": "Kahncode/fact-web", "role": "ui", "default_branch": "main", "merge_order": 1},
        ],
    },
    {"id": 2, "name": "other", "home_repo": "Kahncode/other", "repositories": []},
]


def _use_transport(monkeypatch: pytest.MonkeyPatch, settings: Settings, handler: httpx.MockTransport) -> None:
    monkeypatch.setattr(projects, "make_client", lambda: httpx.Client(base_url=settings.api_url, transport=handler))


def test_projects_list(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json=PROJECTS)

    _use_transport(monkeypatch, settings, httpx.MockTransport(handler))

    result = CliRunner().invoke(app, ["projects", "list"])

    assert result.exit_code == 0, result.output
    assert [str(u) for u in seen] == ["http://127.0.0.1:8100/api/v1/projects"]
    lines = result.output.splitlines()
    assert lines[0].split() == ["NAME", "HOME", "REPOSITORIES"]
    assert lines[1].split() == ["fact", "Kahncode/fact", "Kahncode/fact,", "Kahncode/fact-web"]
    assert lines[2].split() == ["other", "Kahncode/other", "-"]


def test_projects_list_empty(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_transport(monkeypatch, settings, httpx.MockTransport(lambda _: httpx.Response(200, json=[])))
    result = CliRunner().invoke(app, ["projects", "list"])
    assert result.exit_code == 0
    assert result.output.strip() == "No projects."


def test_projects_list_api_down(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        msg = "refused"
        raise httpx.ConnectError(msg, request=request)

    _use_transport(monkeypatch, settings, httpx.MockTransport(handler))
    result = CliRunner().invoke(app, ["projects", "list"])
    assert result.exit_code == 1
    assert "cannot reach the API" in result.output


def test_projects_list_api_error(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_transport(monkeypatch, settings, httpx.MockTransport(lambda _: httpx.Response(500, text="boom")))
    result = CliRunner().invoke(app, ["projects", "list"])
    assert result.exit_code == 1
    assert "API answered 500" in result.output


def test_format_table_aligns_columns() -> None:
    assert projects.format_table(("A", "BB"), [("long", "x")]).splitlines() == ["A     BB", "long  x"]


def test_projects_list_unexpected_body(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_transport(monkeypatch, settings, httpx.MockTransport(lambda _: httpx.Response(200, text="<html>")))
    result = CliRunner().invoke(app, ["projects", "list"])
    assert result.exit_code == 1
    assert "unexpected API response" in result.output
