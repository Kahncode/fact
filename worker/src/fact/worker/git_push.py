"""
Push a run's branch from its clone through `Runner.exec`: on the host (`LocalRunner`) or in its container (`docker`).

The push runs `git lfs push origin <branch>` first, then `git push --no-verify origin <branch>`: LFS objects upload
explicitly, so the repository's pre-push hooks (where `git lfs` would otherwise upload) never run. A project's checks
belong in `pre-commit` hooks or check steps.

The write token reaches the two commands only through their environment: as `GITHUB_TOKEN` (which the `docker` pool's
credential helper reads, and which keeps the runner from adding its read token) and as an `http.<url>.extraHeader`
for `https://github.com/` in `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`/`GIT_CONFIG_VALUE_n`. It is never in argv, never
written to a git config file, and is redacted from every output line handed to the caller.

`run_git` runs git on the host, for the factory's own checkouts (`loader.py`), never for a run's clone.
"""

import base64
from collections.abc import Callable, Mapping
from pathlib import Path

from fact.worker.runners.base import ExecExit, ExecLine, RunnerError
from fact.worker.runners.docker import TOKEN_ENV
from fact.worker.runners.process import build_env, spawn, stream
from fact.worker.workspace_git import WorkspaceGit

GIT_TIMEOUT = 600.0
REDACTED = "***"
GITHUB_URL = "https://github.com/"  # the write token's header goes to GitHub only


class PushError(RuntimeError):
    """`git push`, `git lfs push` or a git query in the clone failed."""


class PushRefusedError(PushError):
    """The branch must not be pushed, such as the repository's default branch."""


def auth_header(token: str) -> str:
    """Build the header GitHub accepts for an installation token over HTTPS."""
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    return f"Authorization: Basic {basic}"


def redactor(token: str | None) -> Callable[[str], str]:
    """Hide the token, raw or inside its Basic header, from a line of output."""
    secrets = [] if not token else [token, auth_header(token).rpartition(" ")[2]]

    def redact(text: str) -> str:
        for secret in secrets:
            text = text.replace(secret, REDACTED)
        return text

    return redact


def push_config(url: str, token: str | None) -> dict[str, str]:
    """
    Environment for `git push`: the token as an `http.<url>.extraHeader`, and no credential helper or prompt.

    The token normally authenticates on its own. If GitHub rejects it, clearing the helpers and askpass (including
    the `GIT_ASKPASS` that VS Code's terminal sets) makes the push fail, instead of prompting or pushing as the user.
    """
    entries = [("core.askPass", ""), ("credential.helper", "")]  # an empty value resets the helper list
    if token:
        entries.append((f"http.{url}.extraHeader", auth_header(token)))
    env = {"GIT_CONFIG_COUNT": str(len(entries)), "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}
    for i, (key, value) in enumerate(entries):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


async def run_git(
    args: list[str], cwd: Path, env: Mapping[str, str] | None = None, redact: Callable[[str], str] = str
) -> tuple[int, list[str]]:
    """Run `git <args>` and return its exit code and its merged output lines, each passed through `redact`."""
    proc = await spawn(["git", *args], cwd, build_env({"GIT_TERMINAL_PROMPT": "0", **(env or {})}))
    lines: list[str] = []
    code = -1
    async for item in stream(proc, GIT_TIMEOUT):
        if isinstance(item, ExecLine):
            lines.append(redact(item.text))
        elif isinstance(item, ExecExit):
            code = item.exit_code
    return code, lines


def push_env(token: str | None) -> dict[str, str]:
    """Environment of the push commands: the write token, and no credential prompt."""
    env = push_config(GITHUB_URL, token)
    if token:
        env[TOKEN_ENV] = token
    return env


async def current_branch(git: WorkspaceGit) -> str:
    """Return the branch checked out in the clone; `PushError` on a detached HEAD."""
    try:
        return await git.value("symbolic-ref", "--quiet", "--short", "HEAD")
    except RunnerError as exc:
        raise PushError(str(exc)) from exc


async def _has_lfs(git: WorkspaceGit) -> bool:
    """
    Whether `git lfs` runs where the clone is.

    Without it nothing was ever cleaned into LFS there, so a branch made there has no LFS object to upload.
    """
    return await git.ok("lfs", "version")


async def _run(
    git: WorkspaceGit, args: list[str], env: dict[str, str], log: Callable[[str], None], redact: Callable[[str], str]
) -> None:
    log(f"$ git {' '.join(args)}")
    code, lines = await git(*args, env=env, redact=redact)
    for line in lines:
        log(line)
    if code != 0:
        msg = f"git {args[0]} {args[1]} failed ({code}): {' '.join(lines)[-1000:]}"
        raise PushError(msg)


async def push_branch(
    git: WorkspaceGit,
    branch: str,
    *,
    default_branch: str,
    token: str | None,
    log: Callable[[str], None],
) -> str:
    """
    Push `branch` to the clone's `origin`, LFS objects first, without pre-push hooks; return the commit pushed.

    Refuses the default branch and a branch not checked out in the clone. Output lines go to `log`, redacted.
    """
    if branch == default_branch:
        msg = f"refusing to push {branch!r}: it is the repository's default branch"
        raise PushRefusedError(msg)
    if not branch.strip() or branch.startswith("-"):
        msg = f"refusing to push invalid branch name {branch!r}"
        raise PushRefusedError(msg)
    checked_out = await current_branch(git)
    if checked_out != branch:
        msg = f"the clone is on {checked_out!r}, not the run's branch {branch!r}"
        raise PushRefusedError(msg)
    env, redact = push_env(token), redactor(token)
    if await _has_lfs(git):
        await _run(git, ["lfs", "push", "origin", branch], env, log, redact)
    else:
        log("git-lfs is not installed here: no LFS object to push")
    await _run(
        git,
        ["push", "--no-verify", "--porcelain", "origin", f"refs/heads/{branch}:refs/heads/{branch}"],
        env,
        log,
        redact,
    )
    return await git.value("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}")
