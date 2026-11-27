from collections.abc import Callable

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
    _use_transport(monkeypatch, settings, httpx.MockTransport(lambda _: httpx.Response(200, json=PROJECTS)))

    result = CliRunner().invoke(app, ["projects", "list"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].split() == ["NAME", "HOME", "REPOSITORIES"]
    assert lines[1].split() == ["fact", "Kahncode/fact", "Kahncode/fact,", "Kahncode/fact-web"]
    assert lines[2].split() == ["other", "Kahncode/other", "-"]


def test_projects_list_empty(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_transport(monkeypatch, settings, httpx.MockTransport(lambda _: httpx.Response(200, json=[])))
    result = CliRunner().invoke(app, ["projects", "list"])
    assert result.exit_code == 0
    assert result.output.strip() == "No projects."


def _refuse(request: httpx.Request) -> httpx.Response:
    msg = "refused"
    raise httpx.ConnectError(msg, request=request)


@pytest.mark.parametrize(
    ("handler", "shown"),
    [
        (_refuse, "cannot reach the API"),
        (lambda _: httpx.Response(500, text="boom"), "API answered 500"),
        (lambda _: httpx.Response(200, text="<html>"), "unexpected API response"),
    ],
    ids=["api_down", "api_error", "unexpected_body"],
)
def test_projects_list_failures_exit_1(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response], shown: str
) -> None:
    _use_transport(monkeypatch, settings, httpx.MockTransport(handler))
    result = CliRunner().invoke(app, ["projects", "list"])
    assert result.exit_code == 1
    assert shown in result.output
