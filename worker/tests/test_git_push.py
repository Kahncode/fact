"""`push_branch` through a real `LocalRunner` to a local bare remote: LFS first, no pre-push hook, the token in env only."""

import shutil
import subprocess
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path

import pytest
from fact.core.settings import Settings
from fact.core.types import WorkspaceHandle
from fact.worker.git_push import PushError, PushRefusedError, auth_header, push_branch, push_config, redactor
from fact.worker.runners import ExecOutput, LocalRunner
from fact.worker.workspace_git import WorkspaceGit

TOKEN = "ghs_push_secret_123"  # noqa: S105 - fake
BRANCH = "fact/task-m1-11"
SECRETS = [TOKEN, auth_header(TOKEN).rpartition(" ")[2]]


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)  # noqa: S603, S607
    return result.stdout.strip()


def _has_lfs() -> bool:
    return shutil.which("git-lfs") is not None


class RecordingRunner(LocalRunner):
    """The local runner, recording each command's argv and the env its caller gave."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def exec(
        self,
        handle: WorkspaceHandle,
        argv: Sequence[str],
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[ExecOutput]:
        self.calls.append((list(argv), dict(env or {})))
        return super().exec(handle, argv, cwd, env, timeout)


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    _git("init", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    return bare


@pytest.fixture
def worktree(tmp_path: Path, remote: Path) -> Path:
    work = tmp_path / "work"
    _git("clone", remote.as_uri(), str(work), cwd=tmp_path)
    _git("config", "user.name", "t", cwd=work)
    _git("config", "user.email", "t@example.com", cwd=work)
    _git("config", "commit.gpgsign", "false", cwd=work)
    _git("checkout", "-b", BRANCH, cwd=work)
    (work / "a.txt").write_text("a\n", encoding="utf-8")
    _git("add", ".", cwd=work)
    _git("commit", "-m", "work", cwd=work)
    return work


@pytest.fixture
def runner(settings: Settings) -> RecordingRunner:
    return RecordingRunner(settings)


def _clone(runner: LocalRunner, worktree: Path) -> WorkspaceGit:
    handle = WorkspaceHandle(run_id="r1", root=str(worktree.parent), task_queue="sandbox", repos={"a/b": str(worktree)})
    return WorkspaceGit(runner, handle, str(worktree))


async def test_push_to_bare_remote(runner: RecordingRunner, worktree: Path, remote: Path) -> None:
    lines: list[str] = []

    sha = await push_branch(_clone(runner, worktree), BRANCH, default_branch="main", token=None, log=lines.append)

    assert sha == _git("rev-parse", f"refs/heads/{BRANCH}", cwd=remote) == _git("rev-parse", "HEAD", cwd=worktree)
    assert any(line.startswith("$ git push --no-verify") for line in lines)


@pytest.mark.parametrize(
    ("checked_out", "branch", "match"),
    [
        ("main", "main", "default branch"),
        (BRANCH, "--force", "invalid branch"),
        ("release/1.2", BRANCH, "not the run's branch"),
    ],
)
async def test_push_refuses(  # noqa: PLR0913, PLR0917
    runner: RecordingRunner, worktree: Path, remote: Path, checked_out: str, branch: str, match: str
) -> None:
    _git("checkout", "-B", checked_out, cwd=worktree)

    with pytest.raises(PushRefusedError, match=match):
        await push_branch(_clone(runner, worktree), branch, default_branch="main", token=None, log=print)
    assert _git("for-each-ref", cwd=remote) == ""


async def test_pre_push_hook_does_not_run(
    runner: RecordingRunner, worktree: Path, remote: Path, tmp_path: Path
) -> None:
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    marker = tmp_path / "hook-ran"
    hook = hooks / "pre-push"
    hook.write_text(f"#!/bin/sh\ntouch '{marker.as_posix()}'\nexit 1\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    _git("config", "core.hooksPath", str(hooks), cwd=worktree)

    await push_branch(_clone(runner, worktree), BRANCH, default_branch="main", token=TOKEN, log=print)

    assert not marker.exists()
    assert _git("rev-parse", f"refs/heads/{BRANCH}", cwd=remote) == _git("rev-parse", "HEAD", cwd=worktree)


@pytest.mark.skipif(not _has_lfs(), reason="needs git-lfs")
async def test_lfs_objects_are_pushed(runner: RecordingRunner, worktree: Path, remote: Path) -> None:
    _git("lfs", "install", "--local", cwd=worktree)  # also installs git-lfs's own pre-push hook, which must not run
    _git("lfs", "track", "*.bin", cwd=worktree)
    (worktree / "model.bin").write_bytes(b"\x00weights\x01" * 64)
    _git("add", ".gitattributes", "model.bin", cwd=worktree)
    _git("commit", "-m", "add a model", cwd=worktree)
    oid = _git("lfs", "ls-files", "--long", cwd=worktree).split()[0]

    await push_branch(_clone(runner, worktree), BRANCH, default_branch="main", token=None, log=print)

    assert (remote / "lfs" / "objects" / oid[:2] / oid[2:4] / oid).is_file()
    assert _git("rev-parse", f"refs/heads/{BRANCH}", cwd=remote) == _git("rev-parse", "HEAD", cwd=worktree)


async def test_the_token_is_only_in_the_push_commands_env(
    runner: RecordingRunner, worktree: Path, remote: Path
) -> None:
    lines: list[str] = []

    await push_branch(_clone(runner, worktree), BRANCH, default_branch="main", token=TOKEN, log=lines.append)

    with_token = [argv[1:3] for argv, env in runner.calls if any(s in v for s in SECRETS for v in env.values())]
    expected = [["push", "--no-verify"]]
    if _has_lfs():
        expected.insert(0, ["lfs", "push"])
    assert with_token == expected
    assert not any(s in arg for s in SECRETS for argv, _ in runner.calls for arg in argv)
    assert not any(s in line for s in SECRETS for line in lines)
    for path in [worktree / ".git" / "config", remote / "config"]:
        assert not any(s in path.read_text(encoding="utf-8") for s in SECRETS)


async def test_failed_push_raises(runner: RecordingRunner, worktree: Path, remote: Path) -> None:
    shutil.rmtree(remote)

    with pytest.raises(PushError, match="failed"):
        await push_branch(_clone(runner, worktree), BRANCH, default_branch="main", token=None, log=print)


def test_redactor_hides_token_and_header() -> None:
    redact = redactor(TOKEN)
    header = auth_header(TOKEN)
    assert TOKEN not in redact(f"remote: token {TOKEN}")
    assert header.rpartition(" ")[2] not in redact(header)
    assert redactor(None)("plain") == "plain"


def test_push_config_without_token_has_no_header() -> None:
    env = push_config("https://github.com/a/b.git", None)
    pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
    assert not any("extraHeader" in key for key in pairs)
    assert pairs["credential.helper"] == pairs["core.askPass"] == ""
    assert (env["GIT_TERMINAL_PROMPT"], env["GIT_ASKPASS"]) == ("0", "")  # a rejected token fails, never prompts
