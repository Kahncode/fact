"""Local worktree runner: one git worktree per repository, cut from a local mirror, running as your user."""

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
from fact.worker.runners.mirror import Mirror
from fact.worker.runners.process import build_env, spawn, stream
from fact.worker.runners.workspace_files import (
    WORKSPACE_MD,
    folder,
    make_writable_and_retry,
    mirror_lock,
    run_lock,
    write_workspace_md,
)

log = get_logger(__name__)

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PROFILE = "local-worktree"
GIT_TIMEOUT = 600.0
_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}  # LC_ALL=C: `Mirror` reads git's English messages


def github_url(full_name: str) -> str:
    return f"https://github.com/{full_name}.git"


_SAFE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")


class LocalWorktreeRunner:
    """
    Implements `Runner`. `remote_url` maps `owner/repo` to a clone URL; tests point it at a local bare repo.

    With a `github` App, worktrees commit as the App's bot user (fact[bot]); otherwise, or if that lookup fails, as
    `commit_author_name`/`commit_author_email` from settings.

    Its handles name `task_queue`, the queue every step on the workspace is sent to: the sandbox queue, as the
    workers run on this one machine.

    `factory_root` is the checkout of the factory that is running; `prepare` refuses a workspace inside it or inside
    `pinned_dir`, so an agent never edits the code it runs on.

    It keeps no coordination state in memory: file locks on disk serialise a run's `prepare` and `teardown` and each
    mirror's clone, fetch and branch pick, across worker processes.
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
        self._remote_url = remote_url
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

    def _mirror_path(self, full_name: str) -> Path:
        owner, _, repo = full_name.partition("/")
        if not all(_SAFE_NAME.match(part) for part in (owner, repo)):
            msg = f"repository must be 'owner/repo', got {full_name!r}"
            raise RunnerError(msg)
        return self._settings.repos_dir / owner / repo

    def _root(self, run_id: str) -> Path:
        """Return the run folder, derived from the id so a handle can never point teardown elsewhere."""
        if not _RUN_ID.match(run_id):
            msg = f"invalid run id {run_id!r}"
            raise RunnerError(msg)
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
        names = [folder(r.full_name) for r in spec.repos]
        if len(set(names)) != len(names):
            msg = "two repositories share a folder name; the workspace needs distinct repo names"
            raise RunnerError(msg)
        for repo in spec.repos:
            self._mirror_path(repo.full_name)
            for branch in (repo.branch, repo.default_branch):
                if branch.startswith("-") or not branch.strip():
                    msg = f"invalid branch name {branch!r}"
                    raise RunnerError(msg)
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
            worktree = root / folder(repo.full_name)
            if not (worktree / ".git").is_file():
                return None
            try:
                branch = (await self._git("branch", "--show-current", cwd=worktree)).strip()
            except RunnerError:
                return None
            if not branch:
                return None
            repos[repo.full_name] = str(worktree)
            branches[repo.full_name] = branch
        return self._handle(spec.run_id, root, repos, branches)

    async def _prepare(self, spec: WorkspaceSpec, root: Path) -> WorkspaceHandle:
        # a retry after a crash mid-prepare finds this run's own half-made workspace: start over, keep the branch free
        await self._remove(self._handle(spec.run_id, root, {r.full_name: "" for r in spec.repos}))
        root.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - one directory, instant
        repos: dict[str, str] = {}
        branches: dict[str, str] = {}
        try:
            for repo in spec.repos:
                worktree = root / folder(repo.full_name)
                repos[repo.full_name] = str(worktree)
                branches[repo.full_name] = await self._add_worktree(repo, worktree)
            write_workspace_md(root, spec, branches)
        except BaseException:
            await self._remove(self._handle(spec.run_id, root, repos))
            raise
        log.info("workspace_ready", run_id=spec.run_id, root=str(root), repos=list(repos), branches=branches)
        return self._handle(spec.run_id, root, repos, branches)

    async def sync_mirror(self, full_name: str) -> Path:
        """Clone or fetch the bare mirror of `owner/repo` (remote branches under `origin/*`) and return its path."""
        mirror = self._mirror_path(full_name)
        async with mirror_lock(mirror):
            await Mirror(self._git, self._git_ok, mirror).sync(self._remote_url(full_name))
        return mirror

    async def fetch(self, full_name: str) -> None:
        """Fetch the mirror: its worktrees share its `origin/*` refs."""
        await self.sync_mirror(full_name)

    async def _add_worktree(self, repo: RepoCheckout, worktree: Path) -> str:
        """
        Add the worktree and return its branch: the task branch, or a suffixed one if another workspace holds it.

        A held branch is never reset, so a workspace left behind keeps its unpushed commits.
        """
        mirror = self._mirror_path(repo.full_name)
        name, email = await self._commit_identity()
        async with mirror_lock(mirror):  # runs on one mirror, in any process, must not pick the same free branch
            await Mirror(self._git, self._git_ok, mirror).sync(self._remote_url(repo.full_name))
            remote_branch = f"refs/remotes/origin/{repo.branch}"
            resumed = await self._git_ok("rev-parse", "--verify", "--quiet", remote_branch, cwd=mirror)
            start = f"origin/{repo.branch}" if resumed else f"origin/{repo.default_branch}"
            branch = await Mirror(self._git, self._git_ok, mirror).add_on_free_branch(repo.branch, worktree, start)
            # git config reads the mirror's shared config, which another worker's sync may be rewriting: on Windows
            # that read fails with "Permission denied" unless it too holds the mirror lock
            await self._git("config", "--worktree", "user.name", name, cwd=worktree)
            await self._git("config", "--worktree", "user.email", email, cwd=worktree)
        if branch != repo.branch:
            log.warning("branch_taken", repo=repo.full_name, wanted=repo.branch, branch=branch)
        log.info("worktree_added", repo=repo.full_name, branch=branch, resumed=resumed)
        return branch

    async def _commit_identity(self) -> tuple[str, str]:
        if self._github is not None:
            try:
                bot = await self._github.bot_identity()  # cached by the App after the first lookup
            except (GitHubError, httpx.HTTPError) as exc:
                log.warning("bot_identity_unavailable", error=str(exc) or type(exc).__name__)
            else:
                return bot.name, bot.email
        return self._settings.commit_author_name, self._settings.commit_author_email

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
            await self._remove(handle)

    async def _remove(self, handle: WorkspaceHandle) -> None:
        """Remove the run's worktrees and folder; the caller holds the run lock."""
        root = self._root(handle.run_id)
        for full_name in handle.repos:
            try:
                mirror = self._mirror_path(full_name)
            except RunnerError:
                continue
            if mirror.exists():
                await self._git_ok("worktree", "remove", "--force", str(root / folder(full_name)), cwd=mirror)
                await self._git_ok("worktree", "prune", cwd=mirror)
        if root.exists():  # in a thread: a large tree can take minutes, and activities on this loop must heartbeat
            await asyncio.to_thread(shutil.rmtree, root, onexc=make_writable_and_retry)
        log.info("workspace_removed", run_id=handle.run_id)

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
