"""LocalWorktreeRunner against a local bare repository standing in for GitHub."""

import asyncio
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fact.core.settings import Settings
from fact.core.types import RepoCheckout, WorkspaceSpec
from fact.worker.runners import ExecExit, ExecLine, LocalWorktreeRunner, RunnerError

FULL_NAME = "acme/widget"
TIMEOUT_SLACK = 30


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)  # noqa: S603, S607
    return result.stdout.strip()


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    _git("init", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    _git("clone", str(bare), str(seed), cwd=tmp_path)
    _git("config", "user.name", "seed", cwd=seed)
    _git("config", "user.email", "seed@example.com", cwd=seed)
    (seed / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", ".", cwd=seed)
    _git("commit", "-m", "init", cwd=seed)
    _git("push", "origin", "HEAD:main", cwd=seed)
    return bare


@pytest.fixture
def runner(settings: Settings, remote: Path) -> LocalWorktreeRunner:
    return LocalWorktreeRunner(settings, remote_url=lambda _name: remote.as_uri())


def _spec(run_id: str = "run-1", branch: str = "fact/task-m1-06") -> WorkspaceSpec:
    repo = RepoCheckout(full_name=FULL_NAME, default_branch="main", branch=branch, role="library")
    return WorkspaceSpec(run_id=run_id, task_key="task:M1-06", repos=[repo])


async def _collect(stream) -> tuple[list[str], ExecExit]:  # noqa: ANN001
    items = [i async for i in stream]
    assert isinstance(items[-1], ExecExit)
    return [i.text for i in items if isinstance(i, ExecLine)], items[-1]


async def test_fresh_branch_from_default(runner: LocalWorktreeRunner, settings: Settings) -> None:
    handle = await runner.prepare(_spec())

    worktree = Path(handle.repos[FULL_NAME])
    assert worktree == settings.workspaces_dir / "run-1" / "widget"
    assert (worktree / "README.md").read_text(encoding="utf-8") == "hello\n"
    assert _git("branch", "--show-current", cwd=worktree) == "fact/task-m1-06"
    assert _git("config", "user.name", cwd=worktree) == "fact"
    workspace_md = (Path(handle.root) / "WORKSPACE.md").read_text(encoding="utf-8")
    assert "acme/widget" in workspace_md
    assert "library" in workspace_md
    assert "fact/task-m1-06" in workspace_md
    await runner.teardown(handle)


async def test_resumes_branch_that_exists_remotely(runner: LocalWorktreeRunner, remote: Path, tmp_path: Path) -> None:
    pusher = tmp_path / "pusher"
    _git("clone", str(remote), str(pusher), cwd=tmp_path)
    _git("config", "user.name", "p", cwd=pusher)
    _git("config", "user.email", "p@example.com", cwd=pusher)
    _git("checkout", "-b", "fact/task-m1-06", cwd=pusher)
    (pusher / "work.txt").write_text("in progress\n", encoding="utf-8")
    _git("add", ".", cwd=pusher)
    _git("commit", "-m", "wip", cwd=pusher)
    _git("push", "origin", "fact/task-m1-06", cwd=pusher)

    first = await runner.prepare(_spec(run_id="run-a", branch="fact/other"))  # creates the mirror before the push
    await runner.teardown(first)
    handle = await runner.prepare(_spec(run_id="run-b"))

    assert (Path(handle.repos[FULL_NAME]) / "work.txt").read_text(encoding="utf-8") == "in progress\n"
    await runner.teardown(handle)


async def test_exec_streams_output_and_exit_code(runner: LocalWorktreeRunner) -> None:
    handle = await runner.prepare(_spec())
    code = "import os,sys; print('hi', flush=True); print('err', file=sys.stderr, flush=True); print(os.environ.get('FACT_X')); sys.exit(3)"

    lines, done = await _collect(runner.exec(handle, [sys.executable, "-c", code], cwd="widget", env={"FACT_X": "1"}))

    assert lines == ["hi", "err", "1"]
    assert done == ExecExit(exit_code=3, timed_out=False)
    await runner.teardown(handle)


async def test_exec_rejects_cwd_outside_workspace(runner: LocalWorktreeRunner) -> None:
    handle = await runner.prepare(_spec())

    with pytest.raises(RunnerError):
        runner.exec(handle, [sys.executable, "-c", "pass"], cwd="..")
    await runner.teardown(handle)


_SPAWN_CHILD = """
import subprocess, sys
child = "import time,pathlib\\nwhile True:\\n    pathlib.Path('beat').write_text(str(time.time()))\\n    time.sleep(0.05)"
subprocess.Popen([sys.executable, "-c", child])
print("started", flush=True)
import time
time.sleep(600)
"""


def _beat(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


async def test_timeout_kills_whole_process_tree(runner: LocalWorktreeRunner) -> None:
    handle = await runner.prepare(_spec())
    beat = Path(handle.root) / "beat"

    start = time.monotonic()
    lines, done = await _collect(runner.exec(handle, [sys.executable, "-c", _SPAWN_CHILD], timeout=3))

    assert time.monotonic() - start < TIMEOUT_SLACK
    assert done.timed_out
    assert done.exit_code == -1
    assert lines == ["started"]
    assert beat.exists(), "child never ran"
    before = _beat(beat)
    await asyncio.sleep(0.5)
    assert _beat(beat) == before, "child survived the timeout"
    await runner.teardown(handle)


async def test_teardown_removes_workspace_and_worktree(runner: LocalWorktreeRunner, settings: Settings) -> None:
    handle = await runner.prepare(_spec())

    await runner.teardown(handle)
    await runner.teardown(handle)  # safe twice

    assert not Path(handle.root).exists()  # noqa: ASYNC240
    mirror = settings.repos_dir / "acme" / "widget"
    assert "run-1" not in _git("worktree", "list", cwd=mirror)


async def test_env_is_minimal(runner: LocalWorktreeRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACT_SECRET_TEST", "leak")
    handle = await runner.prepare(_spec())

    lines, _ = await _collect(
        runner.exec(handle, [sys.executable, "-c", "import os; print(os.environ.get('FACT_SECRET_TEST'))"])
    )

    assert lines == ["None"]
    await runner.teardown(handle)


@pytest.mark.parametrize("run_id", ["..", "../x", "a/b", ""])
async def test_rejects_unsafe_run_id(runner: LocalWorktreeRunner, run_id: str) -> None:
    with pytest.raises(RunnerError):
        await runner.prepare(_spec(run_id=run_id))


@pytest.mark.parametrize(("full_name", "branch"), [("acme/..", "b"), ("a/b/c", "b"), (FULL_NAME, "--orphan")])
async def test_rejects_unsafe_repo_or_branch(runner: LocalWorktreeRunner, full_name: str, branch: str) -> None:
    repo = RepoCheckout(full_name=full_name, default_branch="main", branch=branch)
    with pytest.raises(RunnerError):
        await runner.prepare(WorkspaceSpec(run_id="run-x", task_key="task:x", repos=[repo]))


async def test_timeout_when_stdout_closes_but_process_lives(runner: LocalWorktreeRunner) -> None:
    handle = await runner.prepare(_spec())
    code = "import os,time; os.close(1); time.sleep(600)"

    _, done = await _collect(runner.exec(handle, [sys.executable, "-c", code], timeout=2))

    assert done.timed_out
    await runner.teardown(handle)


async def test_multi_repo_workspace(runner: LocalWorktreeRunner) -> None:
    repos = [RepoCheckout(full_name=n, default_branch="main", branch="fact/t") for n in (FULL_NAME, "acme/other")]
    handle = await runner.prepare(WorkspaceSpec(run_id="run-m", task_key="task:m", repos=repos))

    assert sorted(Path(p).name for p in handle.repos.values()) == ["other", "widget"]
    await runner.teardown(handle)


async def test_agent_can_commit_in_worktree(runner: LocalWorktreeRunner, settings: Settings) -> None:
    handle = await runner.prepare(_spec())
    worktree = Path(handle.repos[FULL_NAME])
    (worktree / "new.txt").write_text("x\n", encoding="utf-8")
    _git("add", ".", cwd=worktree)
    _git("commit", "-m", "agent work", cwd=worktree)
    assert _git("log", "-1", "--format=%an", cwd=worktree) == "fact"
    assert _git("rev-parse", "--is-bare-repository", cwd=settings.repos_dir / FULL_NAME) == "true"
    await runner.teardown(handle)


async def test_refuses_workspace_inside_running_factory(settings: Settings, remote: Path) -> None:
    runner = LocalWorktreeRunner(settings, remote_url=lambda _name: remote.as_uri(), factory_root=settings.data_dir)

    with pytest.raises(RunnerError, match="factory checkout"):
        await runner.prepare(_spec())
    assert not settings.workspaces_dir.exists()


class _PinnedOverWorkspaces(Settings):
    @property
    def pinned_dir(self) -> Path:
        return self.data_dir


async def test_refuses_workspace_inside_pinned_dir(settings: Settings, remote: Path) -> None:
    pinned_settings = _PinnedOverWorkspaces(data_dir=settings.data_dir)
    runner = LocalWorktreeRunner(pinned_settings, remote_url=lambda _name: remote.as_uri())

    with pytest.raises(RunnerError, match="pinned_dir"):
        await runner.prepare(_spec())
    assert not settings.workspaces_dir.exists()


async def test_taken_branch_gets_a_suffix(runner: LocalWorktreeRunner) -> None:
    first = await runner.prepare(_spec(run_id="run-a"))
    second = await runner.prepare(_spec(run_id="run-b"))
    third = await runner.prepare(_spec(run_id="run-c"))

    assert first.branches[FULL_NAME] == "fact/task-m1-06"
    assert second.branches[FULL_NAME] == "fact/task-m1-06-2"
    assert third.branches[FULL_NAME] == "fact/task-m1-06-3"
    assert _git("branch", "--show-current", cwd=Path(second.repos[FULL_NAME])) == "fact/task-m1-06-2"
    assert "fact/task-m1-06-2" in (Path(second.root) / "WORKSPACE.md").read_text(encoding="utf-8")
    for handle in (first, second, third):
        await runner.teardown(handle)


async def test_branch_is_free_again_after_teardown(runner: LocalWorktreeRunner) -> None:
    first = await runner.prepare(_spec(run_id="run-a"))
    await runner.teardown(first)
    second = await runner.prepare(_spec(run_id="run-b"))

    assert second.branches[FULL_NAME] == "fact/task-m1-06"
    await runner.teardown(second)


async def test_branch_is_free_once_its_folder_is_gone(runner: LocalWorktreeRunner) -> None:
    first = await runner.prepare(_spec(run_id="run-a"))
    shutil.rmtree(first.root, onexc=_writable)  # a folder deleted by hand: git still lists the worktree
    second = await runner.prepare(_spec(run_id="run-b"))

    assert second.branches[FULL_NAME] == "fact/task-m1-06"
    await runner.teardown(second)


async def test_suffix_skips_branches_on_the_remote(runner: LocalWorktreeRunner, remote: Path, tmp_path: Path) -> None:
    pusher = tmp_path / "pusher"
    _git("clone", str(remote), str(pusher), cwd=tmp_path)
    _git("push", "origin", "HEAD:refs/heads/fact/task-m1-06-2", cwd=pusher)  # another run's PR branch
    first = await runner.prepare(_spec(run_id="run-a"))
    second = await runner.prepare(_spec(run_id="run-b"))

    assert second.branches[FULL_NAME] == "fact/task-m1-06-3"
    for handle in (first, second):
        await runner.teardown(handle)


def _writable(func, path: str, _exc: BaseException) -> None:  # noqa: ANN001
    Path(path).chmod(stat.S_IWRITE)
    func(path)


async def test_retry_after_a_crash_mid_prepare_keeps_the_branch(runner: LocalWorktreeRunner) -> None:
    await runner.prepare(_spec(run_id="run-a"))  # the activity died before reporting: the retry prepares again
    handle = await runner.prepare(_spec(run_id="run-a"))

    assert handle.branches[FULL_NAME] == "fact/task-m1-06"
    await runner.teardown(handle)


async def test_held_branch_keeps_its_unpushed_commits(runner: LocalWorktreeRunner) -> None:
    left = await runner.prepare(_spec(run_id="run-a"))  # a failed run's workspace, left on disk
    worktree = Path(left.repos[FULL_NAME])
    (worktree / "local.txt").write_text("not pushed\n", encoding="utf-8")
    _git("add", ".", cwd=worktree)
    _git("commit", "-m", "local work", cwd=worktree)
    head = _git("rev-parse", "HEAD", cwd=worktree)

    other = await runner.prepare(_spec(run_id="run-b"))

    assert other.branches[FULL_NAME] == "fact/task-m1-06-2"
    assert _git("rev-parse", "fact/task-m1-06", cwd=worktree) == head
    for handle in (left, other):
        await runner.teardown(handle)


async def test_concurrent_runs_get_distinct_branches(runner: LocalWorktreeRunner) -> None:
    first, second = await asyncio.gather(runner.prepare(_spec(run_id="run-a")), runner.prepare(_spec(run_id="run-b")))

    assert {first.branches[FULL_NAME], second.branches[FULL_NAME]} == {"fact/task-m1-06", "fact/task-m1-06-2"}
    for handle in (first, second):
        await runner.teardown(handle)


async def test_overlapping_attempts_of_one_run_take_turns(runner: LocalWorktreeRunner) -> None:
    first, second = await asyncio.gather(runner.prepare(_spec(run_id="run-a")), runner.prepare(_spec(run_id="run-a")))

    assert first.branches[FULL_NAME] == second.branches[FULL_NAME] == "fact/task-m1-06"
    assert Path(second.repos[FULL_NAME]).exists()  # noqa: ASYNC240
    await runner.teardown(second)
