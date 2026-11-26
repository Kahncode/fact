"""`plan_starts`: dependency depth then id, within the overall and per-project caps; and `FileTaskSource.depths`."""

from pathlib import Path

from fact.core.scheduler import ProjectLoad, ReadyTask, ReadyTasks, plan_starts
from fact.core.tasks.files import FileTaskSource


def _task(task_id: str, depth: int = 0, project: str = "a") -> ReadyTask:
    return ReadyTask(project=project, task_key=f"task:{task_id}", task_id=task_id, depth=depth)


def _ready(tasks: list[ReadyTask], *, max_runs: int = 10, active: int = 0, **projects: tuple[int, int]) -> ReadyTasks:
    loads = [ProjectLoad(project=p, active=a, max_runs=m) for p, (a, m) in projects.items()]
    return ReadyTasks(tasks=tasks, projects=loads, active=active, max_runs=max_runs)


def _ids(chosen: list[ReadyTask]) -> list[str]:
    return [t.task_id for t in chosen]


def test_orders_by_depth_then_id() -> None:
    tasks = [_task("M2-10"), _task("M1-03", depth=1), _task("M2-02"), _task("M10-01"), _task("odd")]
    assert _ids(plan_starts(_ready(tasks, a=(0, 10)))) == ["M2-02", "M2-10", "M10-01", "odd", "M1-03"]


def test_overall_cap_counts_active_runs() -> None:
    tasks = [_task("M1-01"), _task("M1-02"), _task("M1-03")]
    assert _ids(plan_starts(_ready(tasks, max_runs=2, a=(0, 5)))) == ["M1-01", "M1-02"]
    assert _ids(plan_starts(_ready(tasks, max_runs=2, active=1, a=(1, 5)))) == ["M1-01"]
    assert plan_starts(_ready(tasks, max_runs=2, active=2, a=(0, 5))) == []


def test_project_cap_skips_to_other_projects() -> None:
    tasks = [_task("M1-01", project="a"), _task("M1-02", project="a"), _task("M1-03", project="b")]
    chosen = plan_starts(_ready(tasks, max_runs=5, active=1, a=(1, 2), b=(0, 2)))
    assert [(t.project, t.task_id) for t in chosen] == [("a", "M1-01"), ("b", "M1-03")]


def test_unknown_project_gets_nothing() -> None:
    assert plan_starts(_ready([_task("M1-01", project="gone")], a=(0, 2))) == []


def _write(root: Path, rel: str, task_id: str, deps: list[str], status: str = "ready") -> None:
    path = root / "tasks" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    front = f"---\nid: {task_id}\ntitle: t\nstatus: {status}\ndepends_on: [{', '.join(deps)}]\n---\n"
    path.write_text(front, encoding="utf-8")


def test_depths(tmp_path: Path) -> None:
    _write(tmp_path, "M1/02-a.md", "M1-02", ["M1-01"])  # M1-01 finished: its file is gone
    _write(tmp_path, "M1/03-b.md", "M1-03", ["M1-02"], status="planned")
    _write(tmp_path, "M1/04-c.md", "M1-04", ["M1-03", "M1-02"])
    _write(tmp_path, "M1/05-x.md", "M1-05", ["M1-06"])  # a cycle is cut, not followed forever
    _write(tmp_path, "M1/06-y.md", "M1-06", ["M1-05"])
    depths = FileTaskSource(tmp_path).depths()
    assert {k: depths[k] for k in ("task:M1-02", "task:M1-03", "task:M1-04")} == {
        "task:M1-02": 0,
        "task:M1-03": 1,
        "task:M1-04": 2,
    }
    assert {depths["task:M1-05"], depths["task:M1-06"]} <= {0, 1}
