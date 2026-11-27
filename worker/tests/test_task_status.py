"""`report_status`: each run event reaches the task's source as labels, comments, PR links or closing."""

from pathlib import Path
from typing import Any

import pytest
from fact.core.activity_contract import ReportStatusInput, TaskEvent
from fact.core.github import GitHubError
from fact.core.settings import ProjectRef, Settings
from fact.core.tasks.events import NEEDS_HUMAN, RUNNING, report
from fact.core.types import PRRef
from fact.worker.activities import task_status
from fact.worker.activities.task_status import TaskStatusActivities
from temporalio.exceptions import ApplicationError

KEY = "task:M1-99"
PR = PRRef(repo="acme/widget", number=3, url="https://github.com/acme/widget/pull/3", head_sha="abc")
PROJECT_FILE = """name: acme
repositories:
  - full_name: acme/widget
    role: app
task_source:
  kind: {kind}
{extra}workflows:
  default: standard
"""


class FakeSource:
    """Records the write-back calls; the reading half of the protocol is not used by `report`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def comment(self, key: str, text: str) -> None:
        self.calls.append(("comment", (key, text)))

    async def set_labels(self, key: str, add: list[str], remove: list[str]) -> None:
        self.calls.append(("set_labels", (key, add, remove)))

    async def link_prs(self, key: str, prs: list[PRRef]) -> None:
        self.calls.append(("link_prs", (key, prs)))

    async def close(self, key: str) -> None:
        self.calls.append(("close", key))


def _spec(event: TaskEvent, **kw: object) -> ReportStatusInput:
    return ReportStatusInput(run_id="r1", project="acme", task_key=KEY, event=event, **kw)


async def _report(event: TaskEvent, **kw: object) -> list[tuple[str, Any]]:
    source = FakeSource()
    await report(source, _spec(event, **kw))  # type: ignore[arg-type]
    return source.calls


ENDED = ("set_labels", (KEY, [], [RUNNING, NEEDS_HUMAN]))


@pytest.mark.parametrize(
    ("event", "kw", "expected"),
    [
        (
            "run_started",
            {"run_url": "http://ui/runs/r1"},
            [("set_labels", (KEY, [RUNNING], [])), ("comment", (KEY, "http://ui/runs/r1"))],
        ),
        ("gate_opened", {"detail": "approve the plan"}, [("set_labels", (KEY, [NEEDS_HUMAN], [])), ("comment", None)]),
        ("gate_answered", {}, [("set_labels", (KEY, [], [NEEDS_HUMAN]))]),
        ("prs_opened", {"prs": [PR]}, [("link_prs", (KEY, [PR]))]),
        ("merged", {}, [("close", KEY), ENDED]),
        ("closed", {"detail": "superseded"}, [ENDED, ("comment", None)]),
        ("stopped", {"detail": "superseded"}, [ENDED, ("comment", None)]),
    ],
)
async def test_event_reaches_the_source(event: TaskEvent, kw: dict[str, Any], expected: list[tuple[str, Any]]) -> None:
    calls = await _report(event, **kw)
    assert [c[0] for c in calls] == [e[0] for e in expected]
    for call, want in zip(calls, expected, strict=True):
        if want[1] is not None and want[0] != "comment":
            assert call == want
        elif want[0] == "comment":  # wording is free; the run, the URL or the reason must be in it
            text = call[1][1]
            assert "r1" in text or kw.get("detail", "") in text or kw.get("run_url", "") in text


def _project(tmp_path: Path, kind: str = "files") -> Settings:
    repo = tmp_path / "widget"
    (repo / ".fact").mkdir(parents=True)
    extra = "  repo: acme/widget\n" if kind == "github" else ""
    (repo / ".fact" / "project.yaml").write_text(PROJECT_FILE.format(kind=kind, extra=extra), encoding="utf-8")
    return Settings(projects=[ProjectRef(name="acme", home="acme/widget", path=repo)])


def _project_settings(tmp_path: Path, kind: str = "files") -> Settings:
    base = _project(tmp_path, kind)
    return Settings(projects=base.projects, github_app_id=None, github_app_private_key_path=None)


@pytest.mark.parametrize("case", ["file_source", "github_without_app", "no_checkout"])
async def test_activity_is_best_effort_where_nothing_can_be_written(tmp_path: Path, case: str) -> None:
    """A task a source cannot write to, or no source at all, never fails the run's step."""
    if case == "no_checkout":
        settings = Settings(projects=[ProjectRef(name="acme", home="acme/widget")])
    else:
        settings = _project_settings(tmp_path, "files" if case == "file_source" else "github")
    await TaskStatusActivities(settings).report_status(_spec("merged"))


async def test_activity_refuses_a_broken_project_config(tmp_path: Path) -> None:
    settings = _project(tmp_path)
    (tmp_path / "widget" / ".fact" / "project.yaml").write_text("name: [", encoding="utf-8")
    with pytest.raises(ApplicationError) as err:
        await TaskStatusActivities(settings).report_status(_spec("run_started"))
    assert err.value.non_retryable


PERMANENT = {404}


@pytest.mark.parametrize("status", [404, 502])
async def test_activity_does_not_retry_a_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    permanent = status in PERMANENT

    async def refused(*_: object) -> None:
        msg = "GitHub said no"
        raise GitHubError(msg, status)

    monkeypatch.setattr(task_status, "report", refused)
    with pytest.raises((ApplicationError, GitHubError)) as err:
        await TaskStatusActivities(_project(tmp_path)).report_status(_spec("run_started"))
    assert isinstance(err.value, ApplicationError) == permanent
    if permanent:
        assert err.value.non_retryable  # type: ignore[union-attr]
