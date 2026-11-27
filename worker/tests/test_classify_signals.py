"""`classify_signals`: review signals from the change set's paths, the API snapshot's diff and the task's state."""

import json
import subprocess
from pathlib import Path

import pytest
from fact.core.activity_contract import ClassifySignalsInput
from fact.core.classification import ChangedPath, ClassifySignal
from fact.core.settings import ProjectRef, Settings
from fact.core.types import WorkspaceHandle
from fact.worker.activities.classify import ClassifyActivities, matches, removes_or_changes
from temporalio.exceptions import ApplicationError

REPO = "acme/widget"
KEY = "task:M1-99"
PROJECT_FILE = """name: acme
repositories:
  - full_name: acme/widget
    role: app
task_source:
  kind: files
  path: tasks
workflows:
  default: standard
classify:
  schema_paths: [core/migrations/**]
  api_paths: [api/openapi.json]
  always_review_paths: [.fact/**, .github/**]
"""
TASK_FILE = "---\nid: M1-99\ntitle: Add a widget\nstatus: {status}\ndepends_on: []\n---\n\n## Goal\n\nA widget.\n"
SNAPSHOT = {"openapi": "3.1.0", "paths": {"/runs": {"get": {"summary": "List runs"}}}}


def _git(*args: str, cwd: Path) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)  # noqa: S603, S607
    assert result.returncode == 0, result.stderr


def _write(root: Path, rel: str, text: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text, encoding="utf-8", newline="\n")


def _snapshot(paths: dict) -> str:
    return json.dumps({**SNAPSHOT, "paths": paths}, indent=2, sort_keys=True) + "\n"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Make the project's registered checkout: its config and its task, still open."""
    root = tmp_path / "project"
    _write(root, ".fact/project.yaml", PROJECT_FILE)
    _write(root, "tasks/M1/99-widget.md", TASK_FILE.format(status="ready"))
    return root


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    """Make the run's worktree: `origin/main` holds the published snapshot, the run's branch starts from it."""
    repo = tmp_path / "widget"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.name", "fact", cwd=repo)
    _git("config", "user.email", "fact@example.com", cwd=repo)
    _git("config", "commit.gpgsign", "false", cwd=repo)
    _write(repo, "api/openapi.json", json.dumps(SNAPSHOT, indent=2, sort_keys=True) + "\n")
    _write(repo, "src/app.py", "print('hi')\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    _git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=repo)
    _git("checkout", "-q", "-b", "fact/task-m1-99", cwd=repo)
    return repo


def _commit(repo: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        _write(repo, rel, text)
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "change", cwd=repo)


async def _classify(project: Path, worktree: Path) -> list[ClassifySignal]:
    settings = Settings(projects=[ProjectRef(name="acme", home=REPO, path=project)])
    handle = WorkspaceHandle(run_id="r1", root=str(worktree.parent), task_queue="sandbox", repos={REPO: str(worktree)})
    spec = ClassifySignalsInput(run_id="r1", project="acme", task_key=KEY, handle=handle)
    return await ClassifyActivities(settings).classify_signals(spec)


def _named(signals: list[ClassifySignal]) -> dict[str, list[str]]:
    return {s.name: [p.path for p in s.paths] for s in signals}


async def test_a_change_raising_nothing(project: Path, worktree: Path) -> None:
    _commit(worktree, {"src/app.py": "print('hello')\n", "docs/x.md": "doc\n"})
    assert await _classify(project, worktree) == []


async def test_a_migration_is_a_schema_change(project: Path, worktree: Path) -> None:
    _commit(worktree, {"core/migrations/versions/0042_add.py": "# migration\n"})
    signals = await _classify(project, worktree)
    assert signals == [
        ClassifySignal(
            name="schema_change", paths=[ChangedPath(repo=REPO, path="core/migrations/versions/0042_add.py")]
        )
    ]


async def test_an_added_route_is_an_api_change_only(project: Path, worktree: Path) -> None:
    paths = {**SNAPSHOT["paths"], "/tasks": {"get": {"summary": "List tasks"}}}
    _commit(worktree, {"api/openapi.json": _snapshot(paths)})
    assert _named(await _classify(project, worktree)) == {"api_change": ["api/openapi.json"]}


async def test_a_removed_route_is_api_breaking(project: Path, worktree: Path) -> None:
    _commit(worktree, {"api/openapi.json": _snapshot({"/tasks": {"get": {"summary": "List tasks"}}})})
    named = _named(await _classify(project, worktree))
    assert named == {"api_change": ["api/openapi.json"], "api_breaking": ["api/openapi.json"]}


async def test_a_changed_route_is_api_breaking(project: Path, worktree: Path) -> None:
    _commit(worktree, {"api/openapi.json": _snapshot({"/runs": {"get": {"summary": "All runs"}}})})
    assert "api_breaking" in _named(await _classify(project, worktree))


async def test_a_github_change_always_needs_review(project: Path, worktree: Path) -> None:
    _commit(worktree, {".github/workflows/ci.yaml": "on: push\n", "src/app.py": "print('x')\n"})
    assert _named(await _classify(project, worktree)) == {"always_review": [".github/workflows/ci.yaml"]}


async def test_rules_come_from_the_project_not_the_branch(project: Path, worktree: Path) -> None:
    _commit(worktree, {".fact/project.yaml": PROJECT_FILE.replace(".fact/**, .github/**", "")})
    assert _named(await _classify(project, worktree)) == {"always_review": [".fact/project.yaml"]}


async def test_a_closed_task(project: Path, worktree: Path) -> None:
    _write(project, "tasks/M1/99-widget.md", TASK_FILE.format(status="done"))
    _commit(worktree, {"src/app.py": "print('x')\n"})
    assert await _classify(project, worktree) == [ClassifySignal(name="task_closed")]


async def test_an_unregistered_project_fails_for_good(worktree: Path) -> None:
    handle = WorkspaceHandle(run_id="r1", root=str(worktree.parent), task_queue="sandbox", repos={REPO: str(worktree)})
    spec = ClassifySignalsInput(run_id="r1", project="acme", task_key=KEY, handle=handle)
    with pytest.raises(ApplicationError) as exc:
        await ClassifyActivities(Settings(projects=[])).classify_signals(spec)
    assert exc.value.non_retryable


def test_matches() -> None:
    assert matches(".fact/workflows/standard.py", [".fact/**"])
    assert matches("core/migrations/versions/1.py", ["core/migrations/**"])
    assert not matches("core/migrations.py", ["core/migrations/**"])
    assert not matches("api/v2/openapi.json", ["api/openapi.json"])


def test_removes_or_changes() -> None:
    old = {"a": [1, 2], "b": {"c": "x"}}
    assert not removes_or_changes(old, {"a": [2, 1, 3], "b": {"c": "x", "d": 1}, "e": 0})
    assert removes_or_changes(old, {"a": [1], "b": {"c": "x"}})
    assert removes_or_changes(old, {"a": [1, 2], "b": {"c": "y"}})
    assert removes_or_changes(old, {"a": [1, 2]})
    assert removes_or_changes(old, None)
