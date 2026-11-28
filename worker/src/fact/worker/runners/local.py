"""Local runner: one full clone per repository in the run's workspace, running as your user."""

import asyncio
import re
import shutil
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path

import httpx
from fact.core.activity_contract import SANDBOX_QUEUE
from fact.core.github import GitHubApp, GitHubError
from fact.core.logging import get_logger
from fact.core.settings import REPO_ROOT, Settings
from fact.core.types import RepoCheckout, WorkspaceHandle, WorkspaceSpec
from fact.worker.runners.base import ExecLine, ExecOutput, RunnerCapabilities, RunnerError
from fact.worker.runners.process import build_env, spawn, stream
from fact.worker.runners.workspace_files import (
    WORKSPACE_MD,
    folder,
    make_writable_and_retry,
    run_lock,
    write_workspace_md,
)

log = get_logger(__name__)

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PROFILE = "local"
GIT_TIMEOUT = 600.0
_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0"}


def github_url(full_name: str) -> str:
    return f"https://github.com/{full_name}.git"


_SAFE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")


def check_full_name(full_name: str) -> None:
    """Refuse anything but `owner/repo` with safe parts: the name becomes a folder of the workspace."""
    owner, _, repo = full_name.partition("/")
    if not all(_SAFE_NAME.match(part) for part in (owner, repo)):
        msg = f"repository must be 'owner/repo', got {full_name!r}"
        raise RunnerError(msg)


def check_repos(spec: WorkspaceSpec) -> None:
    """Refuse repositories sharing a folder name, unsafe names and branch names that git would read as options."""
    names = [folder(r.full_name) for r in spec.repos]
    if len(set(names)) != len(names):
        msg = "two repositories share a folder name; the workspace needs distinct repo names"
        raise RunnerError(msg)
    for repo in spec.repos:
        check_full_name(repo.full_name)
        for branch in (repo.branch, repo.default_branch):
            if branch.startswith("-") or not branch.strip():
                msg = f"invalid branch name {branch!r}"
                raise RunnerError(msg)


def check_run_id(run_id: str) -> None:
    if not _RUN_ID.match(run_id):
        msg = f"invalid run id {run_id!r}"
        raise RunnerError(msg)


async def commit_identity(github: GitHubApp | None, settings: Settings) -> tuple[str, str]:
    """Return the App's bot user (fact[bot]), or the settings' fallback without the App or when the lookup fails."""
    if github is not None:
        try:
            bot = await github.bot_identity()  # cached by the App after the first lookup
        except (GitHubError, httpx.HTTPError) as exc:
            log.warning("bot_identity_unavailable", error=str(exc) or type(exc).__name__)
        else:
            return bot.name, bot.email
    return settings.commit_author_name, settings.commit_author_email


class LocalRunner:
    """
    Implements `Runner`. `remote_url` maps `owner/repo` to a clone URL; tests point it at a local bare repo.

    Each repository is cloned in full into the workspace, so a workspace shares nothing with another: its own
    `.git`, hooks and config. With a `github` App, clones commit as the App's bot user (fact[bot]); otherwise, or if
    that lookup fails, as `commit_author_name`/`commit_author_email` from settings.

    Its handles name `task_queue`, the queue every step on the workspace is sent to: the sandbox queue, as the
    workers run on this one machine.

    `factory_root` is the checkout of the factory that is running; `prepare` refuses a workspace inside it or inside
    `pinned_dir`, so an agent never edits the code it runs on.

    It keeps no coordination state in memory: a file lock on disk serialises a run's `prepare` and `teardown` across
    worker processes.
    """

    def __init__(
        self,
        settings: Settings,
        remote_url: Callable[[str], str] = github_url,
        github: GitHubApp | None = None,
        factory_root: Path = REPO_ROOT,
        task_queue: str = SANDBOX_QUEUE,
    ) -> None:
        self._settings = settings
        self.task_queue = task_queue
        self.remote_url = remote_url
        self._github = github
        self._factory_root = factory_root

    def _handle(
        self, run_id: str, root: Path, repos: dict[str, str], branches: dict[str, str] | None = None
    ) -> WorkspaceHandle:
        return WorkspaceHandle(
            run_id=run_id, root=str(root), task_queue=self.task_queue, repos=repos, branches=branches or {}
        )

    def capabilities(self) -> RunnerCapabilities:
        return RunnerCapabilities(name=PROFILE, isolation="none", runs_as_user=True, supports_resume=True)

    def _root(self, run_id: str) -> Path:
        """Return the run folder, derived from the id so a handle can never point teardown elsewhere."""
        check_run_id(run_id)
        return self._settings.workspaces_dir / run_id

    def _refuse_protected(self, root: Path) -> None:
        """Refuse a workspace inside `pinned_dir` or inside the running factory checkout."""
        resolved = root.resolve()
        for name, protected in (("pinned_dir", self._settings.pinned_dir), ("factory checkout", self._factory_root)):
            if resolved.is_relative_to(protected.resolve()):
                msg = f"workspace {resolved} is inside the {name} {protected}; move data_dir out of it"
                raise RunnerError(msg)

    def _validate(self, spec: WorkspaceSpec) -> Path:
        root = self._root(spec.run_id)
        self._refuse_protected(root)
        check_repos(spec)
        return root

    async def prepare(self, spec: WorkspaceSpec) -> WorkspaceHandle:
        """
        Create the run's workspace, or return the one an earlier attempt of this run finished.

        Attempts of one run take turns on the run lock, even in different processes, and `WORKSPACE.md`, renamed
        into place last, marks a finished workspace: a retry never tears down a workspace another attempt returned.
        """
        root = self._validate(spec)
        async with run_lock(root):
            handle = await self._finished_workspace(spec, root)
            if handle is not None:
                log.info("workspace_reused", run_id=spec.run_id, root=str(root), branches=handle.branches)
                return handle
            return await self._prepare(spec, root)

    async def _finished_workspace(self, spec: WorkspaceSpec, root: Path) -> WorkspaceHandle | None:
        """Read back the handle of a workspace an earlier attempt finished; None if there is none."""
        if not (root / WORKSPACE_MD).is_file():
            return None
        repos: dict[str, str] = {}
        branches: dict[str, str] = {}
        for repo in spec.repos:
            clone = root / folder(repo.full_name)
            if not (clone / ".git").is_dir():
                return None
            try:
                branch = (await self._git("branch", "--show-current", cwd=clone)).strip()
            except RunnerError:
                return None
            if not branch:
                return None
            repos[repo.full_name] = str(clone)
            branches[repo.full_name] = branch
        return self._handle(spec.run_id, root, repos, branches)

    async def _prepare(self, spec: WorkspaceSpec, root: Path) -> WorkspaceHandle:
        await self._remove(root)  # a retry after a crash mid-prepare finds this run's own half-made workspace
        root.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - one directory, instant
        repos: dict[str, str] = {}
        branches: dict[str, str] = {}
        try:
            for repo in spec.repos:
                clone = root / folder(repo.full_name)
                repos[repo.full_name] = str(clone)
                branches[repo.full_name] = await self._clone(repo, clone)
            write_workspace_md(root, spec, branches)
        except BaseException:
            await self._remove(root)
            raise
        log.info("workspace_ready", run_id=spec.run_id, root=str(root), repos=list(repos), branches=branches)
        return self._handle(spec.run_id, root, repos, branches)

    async def _clone(self, repo: RepoCheckout, clone: Path) -> str:
        """
        Clone the repository and check out its task branch: from `origin/<branch>` if a run pushed it, else new.

        The branch is always the task's own: a task has one active run at a time, and a workspace left behind keeps
        its unpushed commits in its own clone.
        """
        await self._git("clone", "--no-checkout", self.remote_url(repo.full_name), str(clone), cwd=clone.parent)
        remote_branch = f"refs/remotes/origin/{repo.branch}"
        resumed = await self._git_ok("rev-parse", "--verify", "--quiet", remote_branch, cwd=clone)
        start = f"origin/{repo.branch}" if resumed else f"origin/{repo.default_branch}"
        await self._git("checkout", "--no-track", "-B", repo.branch, start, cwd=clone)
        name, email = await commit_identity(self._github, self._settings)
        await self._git("config", "user.name", name, cwd=clone)
        await self._git("config", "user.email", email, cwd=clone)
        log.info("repo_cloned", repo=repo.full_name, branch=repo.branch, resumed=resumed)
        return repo.branch

    def exec(
        self,
        handle: WorkspaceHandle,
        argv: Sequence[str],
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[ExecOutput]:
        root = Path(handle.root).resolve()
        workdir = (root / cwd).resolve() if cwd else root
        if not workdir.is_relative_to(root):
            msg = f"cwd {str(cwd)!r} is outside the workspace"
            raise RunnerError(msg)
        return self._exec(argv, workdir, env, timeout)

    async def _exec(
        self,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str] | None,
        timeout: float | None,  # noqa: ASYNC109 - enforced by `stream`
    ) -> AsyncIterator[ExecOutput]:
        if not argv:
            msg = "argv is empty"
            raise RunnerError(msg)
        proc = await spawn(argv, cwd, build_env(env))
        async for item in stream(proc, timeout):
            yield item

    async def teardown(self, handle: WorkspaceHandle) -> None:
        root = self._root(handle.run_id)
        async with run_lock(root):
            await self._remove(root)
        log.info("workspace_removed", run_id=handle.run_id)

    async def _remove(self, root: Path) -> None:
        """Remove the run folder and its clones; the caller holds the run lock."""
        # the rmtree in a thread: a large tree can take minutes, and activities on this loop must heartbeat
        if root.exists():  # noqa: ASYNC240 - one stat, instant
            await asyncio.to_thread(shutil.rmtree, root, onexc=make_writable_and_retry)

    async def _git_ok(self, *args: str, cwd: Path) -> bool:
        try:
            await self._git(*args, cwd=cwd)
        except RunnerError:
            return False
        return True

    async def _git(self, *args: str, cwd: Path) -> str:
        proc = await spawn(["git", *args], cwd, build_env(_GIT_ENV))
        lines = [item.text async for item in stream(proc, GIT_TIMEOUT) if isinstance(item, ExecLine)]
        out = "\n".join(lines)
        if proc.returncode != 0:
            log.warning("git_failed", args=list(args), exit_code=proc.returncode, output=out)
            msg = f"git {' '.join(args)} failed ({proc.returncode}): {out}"
            raise RunnerError(msg)
        return out
