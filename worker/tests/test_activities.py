"""prepare_workspace and shell_step against a local bare repo and the real API app."""

import subprocess
import sys
from pathlib import Path

import pytest
from fact.core.settings import Settings
from fact.core.types import RepoCheckout, WorkspaceHandle, WorkspaceSpec
from fact.worker.activities import shell as shell_module
from fact.worker.activities.shell import ShellActivities, ShellStepInput
from fact.worker.activities.workspace import WorkspaceActivities
from fact.worker.reporting import StepReporter
from fact.worker.runners import LocalWorktreeRunner
from httpx import AsyncClient
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

FULL_NAME = "acme/widget"
PY = sys.executable
BIG = 6000
SUMMARY_MAX = 2048
TAIL_MAX = 4096
MIN_BEATS = 2


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)  # noqa: S603, S607


def _cmd(code: str) -> str:
    """One YAML list item: an argv running this Python snippet."""
    return f"  - [{PY!r}, '-c', {code!r}]\n"


def _repo_yaml(**sections: list[str]) -> str:
    return "".join(f"{name}:\n{''.join(_cmd(c) for c in cmds)}" for name, cmds in sections.items())


@pytest.fixture(scope="module")
def remote(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Read-only for the tests (each clones its own mirror), so one remote serves the whole module."""
    tmp_path = tmp_path_factory.mktemp("remote")
    bare, seed = tmp_path / "remote.git", tmp_path / "seed"
    _git("init", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    _git("clone", str(bare), str(seed), cwd=tmp_path)
    _git("config", "user.name", "seed", cwd=seed)
    _git("config", "user.email", "seed@example.com", cwd=seed)
    (seed / ".fact").mkdir()
    failing = f"print('x' * {BIG}); print('FAILURE HERE'); raise SystemExit(3)"
    text = _repo_yaml(
        lint=["print('lint ok')"],
        test=["print('test one')", failing, "print('never runs')"],
    )
    (seed / ".fact" / "repo.yaml").write_text(text + "build:\n  - ['definitely-not-a-program-xyz']\n", encoding="utf-8")
    _git("add", ".", cwd=seed)
    _git("commit", "-m", "init", cwd=seed)
    _git("push", "origin", "HEAD:main", cwd=seed)
    return bare


@pytest.fixture
def reporter(client: AsyncClient, tmp_path: Path) -> StepReporter:
    return StepReporter("http://test", client, log_dir=tmp_path / "logs")


@pytest.fixture
def shell(settings: Settings, reporter: StepReporter) -> ShellActivities:
    return ShellActivities(LocalWorktreeRunner(settings), reporter)


@pytest.fixture
async def handle(settings: Settings, remote: Path, reporter: StepReporter, run_id: str) -> WorkspaceHandle:
    runner = LocalWorktreeRunner(settings, remote_url=lambda _n: remote.as_uri())
    repo = RepoCheckout(full_name=FULL_NAME, default_branch="main", branch="fact/t")
    spec = WorkspaceSpec(run_id=run_id, task_key="task:M1-07", repos=[repo])
    return await ActivityEnvironment().run(WorkspaceActivities(runner, reporter).prepare_workspace, spec)


def _input(run_id: str, handle: WorkspaceHandle, check: str, **extra: object) -> ShellStepInput:
    return ShellStepInput.model_validate(
        {
            "run_id": run_id,
            "stage_id": check,
            "attempt": 1,
            "handle": handle,
            "repo": FULL_NAME,
            "check": check,
            **extra,
        }
    )


def _write_repo_yaml(handle: WorkspaceHandle, **sections: list[str]) -> None:
    (Path(handle.repos[FULL_NAME]) / ".fact" / "repo.yaml").write_text(_repo_yaml(**sections), encoding="utf-8")


async def test_prepare_workspace_reports_step(client: AsyncClient, run_id: str, handle: WorkspaceHandle) -> None:
    assert (Path(handle.repos[FULL_NAME]) / ".fact" / "repo.yaml").exists()
    (step,) = (await client.get(f"/api/v1/runs/{run_id}")).json()["steps"]
    assert (step["stage_id"], step["kind"], step["outcome"]) == ("prepare", "shell", "passed")


async def test_passing_check(shell: ShellActivities, run_id: str, handle: WorkspaceHandle) -> None:
    result = await ActivityEnvironment().run(shell.shell_step, _input(run_id, handle, "lint"))
    assert result.outcome == "passed"
    assert result.log_ref is not None


@pytest.mark.parametrize(("repos", "name"), [(1, "lint"), (2, f"lint ({FULL_NAME})")])
async def test_check_step_is_named_after_its_check(  # noqa: PLR0913, PLR0917
    shell: ShellActivities, client: AsyncClient, run_id: str, handle: WorkspaceHandle, repos: int, name: str
) -> None:
    if repos > 1:  # a run with several repositories names the repository too
        handle = handle.model_copy(update={"repos": {**handle.repos, "acme/other": handle.root}})
    await ActivityEnvironment().run(shell.shell_step, _input(run_id, handle, "lint", stage_id="checks"))
    steps = (await client.get(f"/api/v1/runs/{run_id}")).json()["steps"]
    assert [s["name"] for s in steps if s["stage_id"] == "checks"] == [name]


async def test_failing_check_stops_and_keeps_output(
    shell: ShellActivities, client: AsyncClient, run_id: str, handle: WorkspaceHandle
) -> None:
    result = await ActivityEnvironment().run(shell.shell_step, _input(run_id, handle, "test"))
    assert result.outcome == "failed_checks"
    assert "FAILURE HERE" in result.summary
    assert "exited with 3" in result.summary
    assert len(result.summary.encode()) <= SUMMARY_MAX
    assert "FAILURE HERE" in result.outputs["failure_output"]
    assert len(result.outputs["failure_output"].encode()) <= TAIL_MAX
    assert result.log_ref is not None
    log = (await client.get(f"/api/v1/artifacts/{result.log_ref}")).text
    assert "test one" in log
    assert "FAILURE HERE" in log
    assert "never runs" not in log


async def test_missing_program_is_failed_check(shell: ShellActivities, run_id: str, handle: WorkspaceHandle) -> None:
    result = await ActivityEnvironment().run(shell.shell_step, _input(run_id, handle, "build"))
    assert result.outcome == "failed_checks"


async def test_branch_repo_yaml_applies(shell: ShellActivities, run_id: str, handle: WorkspaceHandle) -> None:
    _write_repo_yaml(handle, test=["print(1)"])
    result = await ActivityEnvironment().run(shell.shell_step, _input(run_id, handle, "test"))
    assert result.outcome == "passed"


async def test_timeout_is_failed_check(shell: ShellActivities, run_id: str, handle: WorkspaceHandle) -> None:
    _write_repo_yaml(handle, test=["import time; time.sleep(60)"])
    result = await ActivityEnvironment().run(shell.shell_step, _input(run_id, handle, "test", timeout_s=1.0))
    assert result.outcome == "failed_checks"
    assert "timed out" in result.summary


async def test_heartbeats_while_running(
    shell: ShellActivities, run_id: str, handle: WorkspaceHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shell_module, "HEARTBEAT_S", 0.1)
    _write_repo_yaml(handle, test=["import time; time.sleep(1)"])
    beats: list[object] = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *details: beats.append(details)
    assert (await env.run(shell.shell_step, _input(run_id, handle, "test"))).outcome == "passed"
    assert len(beats) >= MIN_BEATS


async def test_unknown_repo_and_missing_config_are_non_retryable(
    shell: ShellActivities, run_id: str, handle: WorkspaceHandle
) -> None:
    other = _input(run_id, handle, "lint", repo="acme/other")
    with pytest.raises(ApplicationError) as err:
        await ActivityEnvironment().run(shell.shell_step, other)
    assert err.value.non_retryable
    (Path(handle.repos[FULL_NAME]) / ".fact" / "repo.yaml").unlink()
    with pytest.raises(ApplicationError) as err2:
        await ActivityEnvironment().run(shell.shell_step, _input(run_id, handle, "lint"))
    assert err2.value.non_retryable
