"""`plan_starts`: priority, depth then id per project, projects in turn, within the caps; `FileTaskSource.depths`."""

from pathlib import Path

from fact.core.scheduler import ProjectLoad, ReadyTask, ReadyTasks, effective_priorities, plan_starts
from fact.core.tasks.files import FileTaskSource
from fact.core.types import Priority


def _task(
    task_id: str, depth: int = 0, project: str = "a", priority: Priority = "medium", waiting_on: tuple[str, ...] = ()
) -> ReadyTask:
    return ReadyTask(
        project=project,
        task_key=f"task:{task_id}",
        task_id=task_id,
        depth=depth,
        priority=priority,
        effective_priority=priority,
        waiting_on=[f"task:{w}" for w in waiting_on],
    )


def _ready(tasks: list[ReadyTask], **projects: tuple[int, int]) -> ReadyTasks:
    loads = [ProjectLoad(project=p, active=a, max_concurrent_runs=m) for p, (a, m) in projects.items()]
    return ReadyTasks(tasks=tasks, projects=loads)


def _ids(chosen: list[ReadyTask]) -> list[str]:
    return [t.task_id for t in chosen]


def test_orders_by_depth_then_id() -> None:
    tasks = [_task("M2-10"), _task("M1-03", depth=1), _task("M2-02"), _task("M10-01"), _task("odd")]
    assert _ids(plan_starts(_ready(tasks, a=(0, 10)))) == ["M2-02", "M2-10", "M10-01", "odd", "M1-03"]


def test_orders_issues_by_number() -> None:
    issues = [ReadyTask(project="a", task_key=f"gh:acme/app#{n}", task_id=n) for n in ("12", "9", "100")]
    assert _ids(plan_starts(_ready(issues, a=(0, 10)))) == ["9", "12", "100"]


def test_priority_orders_before_depth_and_id() -> None:
    tasks = [_task("M1-01", priority="low"), _task("M1-02"), _task("M3-09", depth=2, priority="urgent")]
    assert _ids(plan_starts(_ready(tasks, a=(0, 10)))) == ["M3-09", "M1-02", "M1-01"]


def test_effective_priority_follows_waits_within_the_project() -> None:
    tasks = [
        _task("M1-01", priority="low"),
        _task("M1-02", waiting_on=("M1-01",)),
        _task("M1-03", priority="urgent", waiting_on=("M1-02",)),  # urgent waits on M1-01 through M1-02
        _task("M1-04", priority="low"),
        _task("M1-04", project="b", priority="urgent", waiting_on=("M1-04",)),  # same key, another project
    ]
    raised = {(t.project, t.task_id): t.effective_priority for t in effective_priorities(tasks)}
    assert raised == {
        ("a", "M1-01"): "urgent",
        ("a", "M1-02"): "urgent",
        ("a", "M1-03"): "urgent",
        ("a", "M1-04"): "low",
        ("b", "M1-04"): "urgent",
    }


def test_pool_cap_counts_active_runs() -> None:
    tasks = [_task("M1-01"), _task("M1-02"), _task("M1-03")]
    assert _ids(plan_starts(_ready(tasks, a=(0, 2)))) == ["M1-01", "M1-02"]
    assert _ids(plan_starts(_ready(tasks, a=(1, 2)))) == ["M1-01"]
    assert plan_starts(_ready(tasks, a=(2, 2))) == []


def test_a_full_pool_leaves_other_projects_free() -> None:
    tasks = [_task("M1-01", project="a"), _task("M1-02", project="a"), _task("M1-03", project="b")]
    chosen = plan_starts(_ready(tasks, a=(1, 2), b=(0, 2)))
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
