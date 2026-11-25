"""push_branch against a local bare remote, and the token plumbing it uses."""

import subprocess
from pathlib import Path

import pytest
from fact.worker import git_push
from fact.worker.git_push import (
    PushError,
    PushRefusedError,
    auth_header,
    branch_sha,
    current_branch,
    push_branch,
    push_config,
    redactor,
)

TOKEN = "ghs_push_secret_123"  # noqa: S105 - fake


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)  # noqa: S603, S607
    return result.stdout.strip()


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    _git("init", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    return bare


@pytest.fixture
def worktree(tmp_path: Path, remote: Path) -> Path:
    work = tmp_path / "work"
    _git("clone", str(remote), str(work), cwd=tmp_path)
    _git("config", "user.name", "t", cwd=work)
    _git("config", "user.email", "t@example.com", cwd=work)
    _git("checkout", "-b", "fact/task-m1-11", cwd=work)
    (work / "a.txt").write_text("a\n", encoding="utf-8")
    _git("add", ".", cwd=work)
    _git("commit", "-m", "work", cwd=work)
    return work


async def test_push_without_token_to_bare_remote(worktree: Path, remote: Path) -> None:
    lines: list[str] = []
    branch = await current_branch(worktree)
    assert branch == "fact/task-m1-11"
    await push_branch(worktree, remote.as_uri(), branch, default_branch="main", token=None, log=lines.append)
    assert _git("rev-parse", "refs/heads/fact/task-m1-11", cwd=remote) == await branch_sha(worktree, branch)
    assert lines[0].startswith("$ git push")


async def test_push_refuses_default_branch(worktree: Path, remote: Path) -> None:
    _git("checkout", "-b", "main", cwd=worktree)
    with pytest.raises(PushRefusedError, match="default branch"):
        await push_branch(worktree, remote.as_uri(), "main", default_branch="main", token=None, log=print)
    assert _git("for-each-ref", cwd=remote) == ""


async def test_push_refuses_option_like_branch(worktree: Path, remote: Path) -> None:
    with pytest.raises(PushRefusedError):
        await push_branch(worktree, remote.as_uri(), "--force", default_branch="main", token=None, log=print)


async def test_pre_push_hook_runs(worktree: Path, remote: Path, tmp_path: Path) -> None:
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    marker = tmp_path / "hook-ran"
    hook = hooks / "pre-push"
    hook.write_text(f"#!/bin/sh\ntouch '{marker.as_posix()}'\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    _git("config", "core.hooksPath", str(hooks), cwd=worktree)
    branch = await current_branch(worktree)
    await push_branch(worktree, remote.as_uri(), branch, default_branch="main", token=TOKEN, log=print)
    assert marker.exists()  # git runs as it would for a person


async def test_failed_push_raises(worktree: Path, tmp_path: Path) -> None:
    missing = (tmp_path / "missing.git").as_uri()
    with pytest.raises(PushError, match="failed"):
        await push_branch(worktree, missing, "fact/task-m1-11", default_branch="main", token=None, log=print)


async def test_token_not_in_argv_config_or_log(worktree: Path, remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []
    real_spawn = git_push.spawn

    async def recording_spawn(argv, cwd, env, **kw):  # noqa: ANN001, ANN003, ANN202
        calls.append((list(argv), dict(env)))
        return await real_spawn(argv, cwd, env, **kw)

    monkeypatch.setattr(git_push, "spawn", recording_spawn)
    lines: list[str] = []
    url = remote.as_uri()
    await push_branch(worktree, url, "fact/task-m1-11", default_branch="main", token=TOKEN, log=lines.append)

    secrets = [TOKEN, auth_header(TOKEN).rpartition(" ")[2]]
    argv, env = calls[-1]
    assert not any(s in arg for s in secrets for arg in argv)
    assert f"http.{url}.extraHeader" in env.values()
    assert auth_header(TOKEN) in env.values()  # it travels only in the environment
    config = _git("config", "--list", "--show-origin", cwd=worktree)
    assert not any(s in config for s in secrets)
    for path in [*worktree.joinpath(".git").glob("config*"), remote / "config"]:
        assert not any(s in path.read_text(encoding="utf-8") for s in secrets)
    assert not any(s in line for s in secrets for line in lines)


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
