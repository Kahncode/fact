"""
Push a run's branch from its worktree, in the control zone.

The installation token reaches git only through `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`/`GIT_CONFIG_VALUE_n`, as an
`http.<url>.extraHeader` scoped to the push URL: it is never in argv, never written to a git config file, and is
redacted from every output line handed to the caller.
"""

import base64
from collections.abc import Callable, Mapping
from pathlib import Path

from fact.worker.runners.base import ExecExit, ExecLine
from fact.worker.runners.process import build_env, spawn, stream

GIT_TIMEOUT = 600.0
REDACTED = "***"


class PushError(RuntimeError):
    """`git push` or a git query in the worktree failed."""


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


async def _git_value(args: list[str], cwd: Path) -> str:
    code, lines = await run_git(args, cwd)
    if code != 0:
        msg = f"git {' '.join(args)} failed ({code}): {' '.join(lines)}"
        raise PushError(msg)
    return lines[0].strip() if lines else ""


async def current_branch(worktree: Path) -> str:
    """Return the branch checked out in `worktree`; `PushError` on a detached HEAD."""
    return await _git_value(["symbolic-ref", "--quiet", "--short", "HEAD"], worktree)


async def branch_sha(worktree: Path, branch: str) -> str:
    return await _git_value(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"], worktree)


async def push_branch(  # noqa: PLR0913 - options are keyword-only
    worktree: Path,
    url: str,
    branch: str,
    *,
    default_branch: str,
    token: str | None,
    log: Callable[[str], None],
) -> None:
    """
    Push `branch` from `worktree` to `url`, given explicitly rather than through the worktree's `origin`.

    Refuses the default branch. Git runs with the user's config and hooks; output lines go to `log`, redacted.
    """
    if branch == default_branch:
        msg = f"refusing to push {branch!r}: it is the repository's default branch"
        raise PushRefusedError(msg)
    if not branch.strip() or branch.startswith("-"):
        msg = f"refusing to push invalid branch name {branch!r}"
        raise PushRefusedError(msg)
    redact = redactor(token)
    args = ["push", "--porcelain", url, f"refs/heads/{branch}:refs/heads/{branch}"]
    log(f"$ git push {url} {branch}")
    code, lines = await run_git(args, worktree, push_config(url, token), redact)
    for line in lines:
        log(line)
    if code != 0:
        msg = f"git push of {branch!r} failed ({code}): {' '.join(lines)[-1000:]}"
        raise PushError(msg)
