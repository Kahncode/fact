"""
`fact use <ref>`: pin the factory version that `fact dev --pinned` runs from.

Fetch the factory repository (the source tree this CLI runs from), resolve `<ref>` (`origin/<ref>` first), check that
SHA out as a detached worktree at `pinned_dir/factory/<sha>`, copy the source tree's git-ignored `.env` into it,
`uv sync --locked` in it, and write the SHA to `pinned_dir/current`. The pinned checkout is then self-contained: it
reads its own committed `fact.yaml`, so config always matches the code that reads it. Agents never work there: their
worktrees live under `workspaces_dir`.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer
from fact.cli.removal import make_writable_and_retry
from fact.core.settings import REPO_ROOT, Settings, get_settings

CURRENT_FILE = "current"
FACTORY_SUBDIR = "factory"
DOTENV = ".env"
_SHA = re.compile(r"^[0-9a-f]{40}$")
FETCH_TIMEOUT_S = 120.0
# never wait on a credential prompt (terminal or Git Credential Manager's window): fail, and the caller retries
NO_PROMPT = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}


class PinError(RuntimeError):
    pass


def current_file(settings: Settings) -> Path:
    return settings.pinned_dir / CURRENT_FILE


def factory_checkout(settings: Settings, sha: str) -> Path:
    return settings.pinned_dir / FACTORY_SUBDIR / sha


def venv_python(checkout: Path) -> Path:
    """Return the Python of a checkout's `.venv`, which `fact use` builds with `uv sync`."""
    return checkout / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def current_pin(settings: Settings) -> Path:
    """Return the checkout named by `pinned_dir/current`; raise if there is none."""
    file = current_file(settings)
    sha = file.read_text(encoding="utf-8").strip() if file.is_file() else ""
    if not _SHA.match(sha):
        msg = f"nothing pinned at {file}; pin a version with `fact use <ref>`"
        raise PinError(msg)
    checkout = factory_checkout(settings, sha)
    if not (checkout / "pyproject.toml").is_file():
        msg = f"pinned {sha} is missing at {checkout}; pin it again with `fact use <ref>`"
        raise PinError(msg)
    return checkout


def fetch_argv() -> list[str]:
    return ["git", "fetch", "--prune", "origin"]


def worktree_add_argv(checkout: Path, sha: str) -> list[str]:
    return ["git", "worktree", "add", "--detach", str(checkout), sha]


def sync_argv(uv: str) -> list[str]:
    return [uv, "sync", "--locked"]


def _run(argv: list[str], cwd: Path, timeout: float | None = None) -> str:
    try:
        result = subprocess.run(  # noqa: S603
            argv,
            cwd=cwd,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            env={**os.environ, **NO_PROMPT},
            text=True,
            encoding="utf-8",
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:  # a missing git or uv, or a fetch that hangs
        msg = f"{' '.join(argv)} failed in {cwd}: {exc}"
        raise PinError(msg) from exc
    if result.returncode != 0:
        msg = f"{' '.join(argv)} failed ({result.returncode}) in {cwd}: {(result.stderr or result.stdout).strip()}"
        raise PinError(msg)
    return result.stdout.strip()


def _try(argv: list[str], cwd: Path) -> str | None:
    try:
        return _run(argv, cwd)
    except PinError:
        return None


def resolve_ref(repo: Path, ref: str) -> str:
    """Return the commit SHA of `origin/<ref>`, else of `<ref>` itself (`HEAD` is always the local one)."""
    if ref.startswith("-") or not ref.strip():
        msg = f"invalid ref {ref!r}"
        raise PinError(msg)
    candidates = [ref] if ref == "HEAD" else [f"refs/remotes/origin/{ref}", ref]  # HEAD: what is checked out here
    for candidate in candidates:
        sha = _try(["git", "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"], repo)
        if sha and _SHA.match(sha):
            return sha
    msg = f"cannot resolve {ref!r} in {repo}"
    raise PinError(msg)


def _checkout(repo: Path, checkout: Path, sha: str) -> None:
    """Create the detached worktree at `checkout`, or keep it when it is already at `sha`."""
    if (checkout / ".git").exists() and _try(["git", "rev-parse", "HEAD"], checkout) == sha:
        return
    if checkout.exists():
        try:
            shutil.rmtree(checkout, onexc=make_writable_and_retry)
        except OSError as exc:
            msg = f"cannot remove stale checkout {checkout}: {exc}; stop `fact dev --pinned` and retry"
            raise PinError(msg) from exc
    _run(["git", "worktree", "prune"], repo)
    checkout.parent.mkdir(parents=True, exist_ok=True)
    _run(worktree_add_argv(checkout, sha), repo)


def latest(ref: str, repo: Path = REPO_ROOT) -> str:
    """Fetch `origin` and return the SHA `ref` names now."""
    _run(fetch_argv(), repo, timeout=FETCH_TIMEOUT_S)
    return resolve_ref(repo, ref)


def github_url(full_name: str) -> str:
    return f"https://github.com/{full_name}.git"


def remote_head(url: str, cwd: Path) -> str:
    """Return the SHA the remote's `HEAD` (its default branch) points at, as the worker resolves a project's home."""
    for line in _run(["git", "ls-remote", url, "HEAD"], cwd, timeout=FETCH_TIMEOUT_S).splitlines():
        sha, _, ref = line.partition("	")
        if ref.strip() == "HEAD" and _SHA.match(sha):
            return sha
    msg = f"{url} has no default branch (empty repository?)"
    raise PinError(msg)


def changed(repo: Path, old: str, new: str, paths: list[str]) -> bool:
    """Return whether any of `paths` differs between commits `old` and `new` (both fetched into `repo`)."""
    try:
        _run(["git", "diff", "--quiet", old, new, "--", *paths], repo)
    except PinError:  # exit code 1: they differ; anything else (an unknown commit) counts as changed too
        return True
    return False


def copy_secrets(repo: Path, checkout: Path) -> None:
    """Copy `repo/.env` into `checkout` on every pin, so a changed secret reaches the next pin; none, nothing."""
    source = repo / DOTENV
    if source.is_file():
        shutil.copyfile(source, checkout / DOTENV)


def prepare(settings: Settings, ref: str, uv: str, repo: Path = REPO_ROOT) -> Path:
    """Fetch, check `ref` out under `pinned_dir/factory/<sha>` and sync it, leaving `current` alone; return it."""
    sha = latest(ref, repo)
    checkout = factory_checkout(settings, sha)
    _checkout(repo, checkout, sha)
    copy_secrets(repo, checkout)
    _run(sync_argv(uv), checkout)
    return checkout


def set_current(settings: Settings, sha: str) -> None:
    current_file(settings).write_text(f"{sha}\n", encoding="utf-8", newline="\n")


def pin(settings: Settings, ref: str, uv: str, repo: Path = REPO_ROOT) -> Path:
    """Prepare `ref`'s checkout and point `current` at it; return the checkout."""
    checkout = prepare(settings, ref, uv, repo)
    set_current(settings, checkout.name)
    return checkout


def use(
    ref: Annotated[str, typer.Argument(help="Branch, tag or SHA; `origin/<ref>` wins after the fetch.")],
) -> None:
    """Pin the factory at <ref> for `fact dev --pinned` to run from."""
    uv = shutil.which("uv")
    if uv is None:
        typer.echo("error: `uv` is not on PATH", err=True)
        raise typer.Exit(1)
    try:
        checkout = pin(get_settings(), ref, uv)
    except PinError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"pinned {checkout.name} at {checkout}")
