"""LocalWorktreeRunner across processes: separate runner instances share only the disk, as two workers do."""

import asyncio
import subprocess
from pathlib import Path

import pytest
from fact.core.settings import Settings
from fact.core.types import RepoCheckout, WorkspaceHandle, WorkspaceSpec
from fact.worker.runners import LocalWorktreeRunner, branches

FULL_NAME = "acme/widget"
BRANCH = "fact/task-m1-06"


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)  # noqa: S603, S607
    return result.stdout.strip()


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    _git("init", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    _git("clone", str(bare), str(seed), cwd=tmp_path)
    (seed / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", ".", cwd=seed)
    _git("-c", "user.name=seed", "-c", "user.email=seed@example.com", "commit", "-m", "init", cwd=seed)
    _git("push", "origin", "HEAD:main", cwd=seed)
    return bare


def _runner(settings: Settings, remote: Path) -> LocalWorktreeRunner:
    return LocalWorktreeRunner(settings, remote_url=lambda _name: remote.as_uri())


def _spec(run_id: str) -> WorkspaceSpec:
    repo = RepoCheckout(full_name=FULL_NAME, default_branch="main", branch=BRANCH)
    return WorkspaceSpec(run_id=run_id, task_key="task:M1-06", repos=[repo])


async def test_two_workers_prepare_two_runs_on_a_new_mirror_at_once(settings: Settings, remote: Path) -> None:
    one, two = _runner(settings, remote), _runner(settings, remote)

    first, second = await asyncio.gather(one.prepare(_spec("run-a")), two.prepare(_spec("run-b")))

    assert {first.branches[FULL_NAME], second.branches[FULL_NAME]} == {BRANCH, f"{BRANCH}-2"}
    mirror = settings.repos_dir / FULL_NAME
    assert _git("config", "remote.origin.fetch", cwd=mirror) == "+refs/heads/*:refs/remotes/origin/*"
    assert not mirror.with_name(".widget.clone").exists()
    for handle in (first, second):
        assert (Path(handle.repos[FULL_NAME]) / "README.md").is_file()
        await one.teardown(handle)


async def test_a_clone_cut_short_is_cloned_again(settings: Settings, remote: Path) -> None:
    partial = settings.repos_dir / "acme" / ".widget.clone"  # a worker killed mid-clone left this
    partial.mkdir(parents=True)
    (partial / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    handle = await _runner(settings, remote).prepare(_spec("run-a"))

    assert (Path(handle.repos[FULL_NAME]) / "README.md").is_file()


async def test_a_second_attempt_never_deletes_the_workspace_the_first_returns(
    settings: Settings, remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale, retry = _runner(settings, remote), _runner(settings, remote)
    inside, release = asyncio.Event(), asyncio.Event()
    add_worktree = stale._add_worktree  # noqa: SLF001

    async def slow_add(repo: RepoCheckout, worktree: Path) -> str:
        branch = await add_worktree(repo, worktree)
        inside.set()
        await release.wait()  # Temporal has given up on this attempt, which still runs
        return branch

    monkeypatch.setattr(stale, "_add_worktree", slow_add)
    first_task = asyncio.create_task(stale.prepare(_spec("run-a")))
    await inside.wait()
    second_task = asyncio.create_task(retry.prepare(_spec("run-a")))
    release.set()
    first: WorkspaceHandle = await first_task
    second = await second_task

    assert second == first
    assert (Path(first.repos[FULL_NAME]) / "README.md").is_file()
    assert (Path(first.root) / "WORKSPACE.md").is_file()


async def test_a_branch_git_finds_checked_out_elsewhere_is_picked_again(
    settings: Settings, remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner(settings, remote)
    held = await runner.prepare(_spec("run-a"))

    async def nothing_held(*_args: object, **_kwargs: object) -> set[str]:
        return set()  # as when a worker without the mirror lock adds a worktree between pick and add

    monkeypatch.setattr(branches, "held_branches", nothing_held)
    other = await runner.prepare(_spec("run-b"))

    assert other.branches[FULL_NAME] == f"{BRANCH}-2"
    assert held.branches[FULL_NAME] == BRANCH
