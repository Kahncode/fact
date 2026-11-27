"""`sync_base` with the real LocalWorktreeRunner, a bare repository standing in for GitHub, reporting to the real API."""

import subprocess
from pathlib import Path

import httpx
import pytest
from fact.core.activity_contract import SyncBaseInput
from fact.core.enums import StepOutcome
from fact.core.settings import Settings
from fact.core.types import RepoCheckout, StepResult, WorkspaceHandle, WorkspaceSpec
from fact.worker.activities.workspace import WorkspaceActivities
from fact.worker.reporting import StepReporter
from fact.worker.runners import LocalWorktreeRunner, RunnerError
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

FULL_NAME = "acme/widget"
REPO = RepoCheckout(full_name=FULL_NAME, default_branch="main", branch="fact/task-m1-99")


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)  # noqa: S603, S607
    return result.stdout.strip()


def _commit(cwd: Path, name: str, text: str, message: str) -> None:
    (cwd / name).write_text(text, encoding="utf-8", newline="\n")
    _git("add", name, cwd=cwd)
    _git("commit", "-m", message, cwd=cwd)


@pytest.fixture
def seed(tmp_path: Path) -> Path:
    """Return a clone of the bare `remote.git` that pushes to `main`, standing in for other PRs merging."""
    bare, seed = tmp_path / "remote.git", tmp_path / "seed"
    _git("init", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    _git("clone", str(bare), str(seed), cwd=tmp_path)
    _git("config", "user.name", "seed", cwd=seed)
    _git("config", "user.email", "seed@example.com", cwd=seed)
    _commit(seed, "README.md", "hello\n", "init")
    _git("push", "origin", "HEAD:main", cwd=seed)
    return seed


@pytest.fixture
def runner(settings: Settings, tmp_path: Path, seed: Path) -> LocalWorktreeRunner:  # noqa: ARG001 - seeds the remote
    remote = tmp_path / "remote.git"
    return LocalWorktreeRunner(settings, remote_url=lambda _name: remote.as_uri())


@pytest.fixture
async def workspace(runner: LocalWorktreeRunner, run_id: str) -> WorkspaceHandle:
    return await runner.prepare(WorkspaceSpec(run_id=run_id, task_key="task:M1-99", repos=[REPO]))


@pytest.fixture
def activities(runner: LocalWorktreeRunner, client: httpx.AsyncClient, tmp_path: Path) -> WorkspaceActivities:
    return WorkspaceActivities(runner, StepReporter("http://test", client, log_dir=tmp_path / "logs"))


async def _sync(
    activities: WorkspaceActivities, handle: WorkspaceHandle, attempt: int = 1, *, verify_only: bool = False
) -> StepResult:
    spec = SyncBaseInput(run_id=handle.run_id, handle=handle, repos=[REPO], attempt=attempt, verify_only=verify_only)
    return await ActivityEnvironment().run(activities.sync_base, spec)


def _worktree(handle: WorkspaceHandle) -> Path:
    return Path(handle.repos[FULL_NAME])


async def test_a_branch_containing_the_base_is_up_to_date(
    activities: WorkspaceActivities, workspace: WorkspaceHandle, client: httpx.AsyncClient
) -> None:
    result = await _sync(activities, workspace)

    assert result.outcome == StepOutcome.PASSED
    assert result.outputs == {"merged": [], "conflicts": {}}
    (step,) = (await client.get(f"/api/v1/runs/{workspace.run_id}")).json()["steps"]
    assert (step["stage_id"], step["kind"], step["outcome"]) == ("sync_base", "shell", "passed")


async def test_a_base_that_moved_on_is_merged_with_a_merge_commit(
    activities: WorkspaceActivities, workspace: WorkspaceHandle, seed: Path
) -> None:
    worktree = _worktree(workspace)
    _commit(worktree, "widget.py", "x = 1\n", "feat: widget")
    _commit(seed, "other.py", "y = 2\n", "feat: other")
    _git("push", "origin", "HEAD:main", cwd=seed)

    result = await _sync(activities, workspace)

    assert result.outcome == StepOutcome.PASSED
    assert result.outputs == {"merged": [FULL_NAME], "conflicts": {}}
    assert (worktree / "other.py").exists()
    assert len(_git("rev-list", "--parents", "-n", "1", "HEAD", cwd=worktree).split()) == 3  # noqa: PLR2004 - a merge
    assert _git("symbolic-ref", "--short", "HEAD", cwd=worktree) == REPO.branch


async def test_a_conflict_is_left_in_progress_then_concluded_once_resolved(
    activities: WorkspaceActivities, workspace: WorkspaceHandle, seed: Path
) -> None:
    worktree = _worktree(workspace)
    _commit(worktree, "README.md", "hello from the task\n", "docs: task")
    _commit(seed, "README.md", "hello from main\n", "docs: main")
    _git("push", "origin", "HEAD:main", cwd=seed)

    conflicted = await _sync(activities, workspace)

    assert conflicted.outcome == StepOutcome.FAILED_CHECKS
    assert conflicted.outputs == {"merged": [], "conflicts": {FULL_NAME: ["README.md"]}}
    assert "<<<<<<<" in (worktree / "README.md").read_text(encoding="utf-8")

    still = await _sync(activities, workspace, attempt=2)  # nothing resolved yet: the same conflict
    assert still.outputs["conflicts"] == {FULL_NAME: ["README.md"]}

    (worktree / "README.md").write_text("hello from main and the task\n", encoding="utf-8", newline="\n")
    _git("add", "README.md", cwd=worktree)  # resolved and staged, as an agent may leave it, but not committed
    concluded = await _sync(activities, workspace, attempt=3)

    assert concluded.outcome == StepOutcome.PASSED
    assert concluded.outputs == {"merged": [FULL_NAME], "conflicts": {}}
    assert _git("status", "--porcelain", cwd=worktree) == ""
    assert (await _sync(activities, workspace, attempt=4)).outputs["merged"] == []  # now up to date


async def test_a_merge_git_refuses_is_not_retried(
    activities: WorkspaceActivities, workspace: WorkspaceHandle, seed: Path
) -> None:
    worktree = _worktree(workspace)
    _commit(seed, "README.md", "hello from main\n", "docs: main")
    _git("push", "origin", "HEAD:main", cwd=seed)
    (worktree / "README.md").write_text("uncommitted\n", encoding="utf-8")  # a dirty file the merge would overwrite

    with pytest.raises(ApplicationError) as caught:
        await _sync(activities, workspace)
    assert caught.value.non_retryable


async def test_concluding_a_merge_does_not_chase_a_base_that_moved_on_meanwhile(
    activities: WorkspaceActivities, workspace: WorkspaceHandle, seed: Path
) -> None:
    worktree = _worktree(workspace)
    _commit(worktree, "README.md", "hello from the task\n", "docs: task")
    _commit(seed, "README.md", "hello from main\n", "docs: main")
    _git("push", "origin", "HEAD:main", cwd=seed)
    await _sync(activities, workspace)
    (worktree / "README.md").write_text("hello from main and the task\n", encoding="utf-8", newline="\n")
    _git("add", "README.md", cwd=worktree)
    _commit(seed, "later.py", "z = 3\n", "feat: later")  # main moves on while the merge is being resolved
    _git("push", "origin", "HEAD:main", cwd=seed)

    result = await _sync(activities, workspace, attempt=2)

    assert result.outputs == {"merged": [FULL_NAME], "conflicts": {}}
    assert not (worktree / "later.py").exists()  # a clean addition squashes fine: GitHub would say otherwise


async def test_a_failed_fetch_is_retried(
    activities: WorkspaceActivities, workspace: WorkspaceHandle, tmp_path: Path
) -> None:
    (tmp_path / "remote.git").rename(tmp_path / "gone.git")  # GitHub unreachable

    with pytest.raises(RunnerError):  # not a non-retryable ApplicationError: Temporal tries again
        await _sync(activities, workspace)


async def test_verifying_a_merge_the_agent_committed_does_not_merge_a_base_that_moved_on(
    activities: WorkspaceActivities, workspace: WorkspaceHandle, seed: Path
) -> None:
    worktree = _worktree(workspace)
    _commit(worktree, "README.md", "hello from the task\n", "docs: task")
    _commit(seed, "README.md", "hello from main\n", "docs: main")
    _git("push", "origin", "HEAD:main", cwd=seed)
    await _sync(activities, workspace)
    (worktree / "README.md").write_text("hello from main and the task\n", encoding="utf-8", newline="\n")
    _git("add", "README.md", cwd=worktree)
    _git("commit", "--no-edit", cwd=worktree)  # the agent concludes the merge itself, as the prompt asks
    _commit(seed, "later.py", "z = 3\n", "feat: later")  # main moves on during the session
    _git("push", "origin", "HEAD:main", cwd=seed)

    result = await _sync(activities, workspace, attempt=2, verify_only=True)

    assert result.outcome == StepOutcome.PASSED
    assert result.outputs == {"merged": [], "conflicts": {}}
    assert not (worktree / "later.py").exists()
