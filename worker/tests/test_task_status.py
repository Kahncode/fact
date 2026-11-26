"""`report_status`: each run event reaches the task's source as labels, comments, PR links or closing."""

from pathlib import Path
from typing import Any

import pytest
from fact.core.activity_contract import ReportStatusInput, TaskEvent
from fact.core.settings import ProjectRef, Settings
from fact.core.tasks.events import NEEDS_HUMAN, RUNNING, report
from fact.core.types import PRRef
from fact.worker.activities.task_status import TaskStatusActivities
from structlog.testing import capture_logs
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


async def test_run_started_labels_and_links_the_run() -> None:
    calls = await _report("run_started", run_url="http://ui/runs/r1")
    assert calls == [
        ("set_labels", (KEY, [RUNNING], [])),
        ("comment", (KEY, "fact started run [r1](http://ui/runs/r1).")),
    ]


async def test_gates_set_and_clear_needs_human() -> None:
    opened = await _report("gate_opened", detail="approve the plan")
    assert opened[0] == ("set_labels", (KEY, [NEEDS_HUMAN], []))
    assert "approve the plan" in opened[1][1][1]
    assert await _report("gate_answered") == [("set_labels", (KEY, [], [NEEDS_HUMAN]))]


async def test_prs_opened_links_them() -> None:
    assert await _report("prs_opened", prs=[PR]) == [("link_prs", (KEY, [PR]))]


async def test_merged_closes_the_task() -> None:
    assert await _report("merged") == [("close", KEY), ("set_labels", (KEY, [], [RUNNING, NEEDS_HUMAN]))]


@pytest.mark.parametrize("event", ["closed", "dropped"])
async def test_closed_or_dropped_clears_labels_and_says_why(event: TaskEvent) -> None:
    calls = await _report(event, detail="superseded")
    assert calls[0] == ("set_labels", (KEY, [], [RUNNING, NEEDS_HUMAN]))
    assert calls[1] == ("comment", (KEY, f"Run r1 ended {event}: superseded."))


def _project(tmp_path: Path, kind: str = "files") -> Settings:
    repo = tmp_path / "widget"
    (repo / ".fact").mkdir(parents=True)
    extra = "  repo: acme/widget\n" if kind == "github" else ""
    (repo / ".fact" / "project.yaml").write_text(PROJECT_FILE.format(kind=kind, extra=extra), encoding="utf-8")
    return Settings(projects=[ProjectRef(name="acme", home="acme/widget", path=repo)])


async def test_activity_dispatches_to_the_file_source(tmp_path: Path) -> None:
    with capture_logs() as logs:
        await TaskStatusActivities(_project(tmp_path)).report_status(_spec("merged"))
    actions = [e.get("action") for e in logs if e["event"] == "task_write_back_skipped"]
    assert actions == ["close", "set_labels"]
    assert any(e["event"] == "task_status_reported" for e in logs)


@pytest.mark.parametrize("kind", ["github", None])
async def test_activity_skips_without_a_writable_source(tmp_path: Path, kind: str | None) -> None:
    settings = _project(tmp_path, kind) if kind else Settings(projects=[ProjectRef(name="acme", home="acme/widget")])
    with capture_logs() as logs:
        await TaskStatusActivities(settings).report_status(_spec("run_started"))
    assert [e["event"] for e in logs] == ["report_status_skipped"]


async def test_activity_refuses_a_broken_project_config(tmp_path: Path) -> None:
    settings = _project(tmp_path)
    (tmp_path / "widget" / ".fact" / "project.yaml").write_text("name: [", encoding="utf-8")
    with pytest.raises(ApplicationError) as err:
        await TaskStatusActivities(settings).report_status(_spec("run_started"))
    assert err.value.non_retryable
