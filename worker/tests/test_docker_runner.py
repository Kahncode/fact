"""DockerRunner against real Docker: a tiny image with a bare repository inside standing in for GitHub."""

import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
import structlog
from docker_env import needs_docker
from fact.core.github import BotIdentity, GitHubApp
from fact.core.project_config import RunnerPoolConfig
from fact.core.settings import Settings
from fact.core.types import RepoCheckout, WorkspaceHandle, WorkspaceSpec
from fact.worker.runners import DockerRunner, ExecExit, ExecLine
from fact.worker.runners.docker_cli import container_name, run_args, volume_name

FULL_NAME = "acme/widget"
RESUMED = "fact/task-resumed"
TOKEN = "ghs_fake_read_token_4f2a"  # noqa: S105 - a fake, looked for where it must never be
DOCKERFILE = """FROM alpine:3
RUN apk add --no-cache git git-lfs && adduser -D -u 1000 fact && mkdir /workspace && chown fact:fact /workspace
COPY --chown=fact:fact remote.git /srv/remote.git
"""

pytestmark = [needs_docker, pytest.mark.timeout(300)]  # the first test builds the image


def _run(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    result = subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, check=check)  # noqa: S603
    return result.stdout.strip()


def _git(*args: str, cwd: Path) -> None:
    _run("git", "-c", "user.name=seed", "-c", "user.email=seed@example.com", *args, cwd=cwd)


@pytest.fixture(scope="module")
def image(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Build the test image: `main` with README.md, and `fact/task-resumed` with work.txt, in /srv/remote.git."""
    context = tmp_path_factory.mktemp("image")
    seed = context / "seed"
    _run("git", "init", "-b", "main", str(seed))
    (seed / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", ".", cwd=seed)
    _git("commit", "-m", "init", cwd=seed)
    _git("checkout", "-b", RESUMED, cwd=seed)
    (seed / "work.txt").write_text("in progress\n", encoding="utf-8")
    _git("add", ".", cwd=seed)
    _git("commit", "-m", "wip", cwd=seed)
    _run("git", "clone", "--bare", str(seed), str(context / "remote.git"))
    (context / "Dockerfile").write_text(DOCKERFILE, encoding="utf-8")
    tag = f"fact-test/docker-runner:{uuid.uuid4().hex[:12]}"
    _run("docker", "build", "--quiet", "--tag", tag, str(context))
    yield tag
    _run("docker", "image", "rm", "--force", tag, check=False)


@dataclass
class _Token:
    token: str
    expires_at: datetime


class FakeApp:
    """The two `GitHubApp` calls the runner makes, with the scopes it asked for recorded."""

    def __init__(self) -> None:
        self.asked: list[tuple[list[str], dict[str, str]]] = []

    async def repositories_token(self, full_names: list[str], permissions: dict[str, str]) -> _Token:
        self.asked.append((list(full_names), dict(permissions)))
        return _Token(TOKEN, datetime.now(UTC))

    async def bot_identity(self) -> BotIdentity:
        return BotIdentity(slug="fact", name="fact[bot]", email="1+fact[bot]@users.noreply.github.com")


@pytest.fixture
def app() -> FakeApp:
    return FakeApp()


@pytest.fixture
def run_id() -> Iterator[str]:
    run = f"t-{uuid.uuid4().hex[:12]}"
    yield run
    _run("docker", "rm", "--force", container_name(run), check=False)
    _run("docker", "volume", "rm", "--force", volume_name(run), check=False)


@pytest.fixture
def runner(settings: Settings, image: str, app: FakeApp) -> DockerRunner:
    pool = RunnerPoolConfig(kind="docker", memory="512m", cpus=1, pids=256)
    return DockerRunner(
        settings,
        {"tiny": image},
        pool,
        remote_url=lambda _name: "file:///srv/remote.git",
        github=cast("GitHubApp", app),
    )


def _spec(run_id: str, branch: str = "fact/task-146") -> WorkspaceSpec:
    repo = RepoCheckout(full_name=FULL_NAME, default_branch="main", branch=branch, role="library")
    return WorkspaceSpec(run_id=run_id, task_key="gh:acme/widget#146", repos=[repo], sandbox="tiny")


async def _collect(stream: AsyncIterator[ExecLine | ExecExit]) -> tuple[list[str], ExecExit]:
    items = [i async for i in stream]
    assert isinstance(items[-1], ExecExit)
    return [i.text for i in items if isinstance(i, ExecLine)], items[-1]


async def _out(runner: DockerRunner, handle: WorkspaceHandle, *argv: str) -> str:
    lines, end = await _collect(runner.exec(handle, list(argv), cwd="widget"))
    assert end.exit_code == 0, lines
    return "\n".join(lines)


def test_run_args_order_and_no_host_mount() -> None:
    pool = RunnerPoolConfig(kind="docker", run_args=["--pool-arg"])
    args = run_args("r1", "img:tag", pool, ["--workflow-arg"])

    assert args[2:6] == ["--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
    assert args.index("--pids-limit") < args.index("--pool-arg") < args.index("--workflow-arg") < args.index("img:tag")
    assert "-v" not in args
    assert "--volume" not in args
    mounts = [args[i + 1] for i, a in enumerate(args) if a == "--mount"]
    assert mounts == ["type=volume,source=fact-ws-r1,target=/workspace"]


async def test_prepare_exec_teardown(runner: DockerRunner, run_id: str, app: FakeApp) -> None:
    with structlog.testing.capture_logs() as logs:
        handle = await runner.prepare(_spec(run_id))
        out = await _out(
            runner, handle, "sh", "-c", 'pwd; cat README.md; git branch --show-current; echo "$GITHUB_TOKEN"'
        )
        config = await _out(runner, handle, "sh", "-c", "cat .git/config ~/.gitconfig; git config user.name")
        workspace_md = await _out(runner, handle, "cat", "/workspace/WORKSPACE.md")

    assert handle.repos == {FULL_NAME: "/workspace/widget"}
    assert out.splitlines() == ["/workspace/widget", "hello", "fact/task-146", TOKEN]
    assert app.asked[-1] == ([FULL_NAME], {"contents": "read"})
    assert "fact/task-146" in workspace_md
    assert "fact[bot]" in config
    assert any(e["event"] == "workspace_ready" for e in logs)
    inspect = _run("docker", "inspect", container_name(run_id))
    for where in (config, inspect, str(logs)):
        assert TOKEN not in where

    await runner.teardown(handle)
    await runner.teardown(handle)
    assert not _run("docker", "ps", "--all", "--quiet", "--filter", f"name={container_name(run_id)}")
    assert not _run("docker", "volume", "ls", "--quiet", "--filter", f"name={volume_name(run_id)}")


async def test_timeout_kills_the_command_inside(runner: DockerRunner, run_id: str) -> None:
    handle = await runner.prepare(_spec(run_id))

    _, end = await _collect(runner.exec(handle, ["sh", "-c", "sleep 300 & sleep 301"], timeout=1))

    assert end == ExecExit(exit_code=-1, timed_out=True)
    top = _run("docker", "top", container_name(run_id))
    assert "sleep 30" not in top


async def test_exec_starts_a_stopped_container(runner: DockerRunner, run_id: str) -> None:
    handle = await runner.prepare(_spec(run_id))
    _run("docker", "stop", "--time", "1", container_name(run_id))

    assert await _out(runner, handle, "cat", "README.md") == "hello"


async def test_prepare_reuses_a_finished_workspace_and_redoes_a_half_made_one(
    runner: DockerRunner, run_id: str
) -> None:
    handle = await runner.prepare(_spec(run_id, RESUMED))
    assert await _out(runner, handle, "cat", "work.txt") == "in progress"  # resumed from origin/<branch>
    await _out(runner, handle, "touch", "marker")

    again = await runner.prepare(_spec(run_id, RESUMED))
    assert again == handle
    await _out(runner, handle, "test", "-f", "marker")  # not cloned again

    await _out(runner, handle, "rm", "/workspace/WORKSPACE.md")  # as if an attempt died before finishing
    await runner.prepare(_spec(run_id, RESUMED))
    lines, end = await _collect(runner.exec(handle, ["test", "-f", "marker"], cwd="widget"))
    assert end.exit_code == 1, lines
