"""Local worktree runner: one git worktree per repository, cut from a local mirror, running as your user."""

import asyncio
import re
import shutil
import stat
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path

import httpx
from fact.core.github import GitHubApp, GitHubError
from fact.core.logging import get_logger
from fact.core.settings import REPO_ROOT, Settings
from fact.core.types import RepoCheckout, WorkspaceHandle, WorkspaceSpec
from fact.worker.runners.base import ExecLine, ExecOutput, RunnerCapabilities, RunnerError
from fact.worker.runners.process import build_env, spawn, stream

log = get_logger(__name__)

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PROFILE = "local-worktree"
GIT_TIMEOUT = 600.0


def github_url(full_name: str) -> str:
    return f"https://github.com/{full_name}.git"


_SAFE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")


def _folder(full_name: str) -> str:
    return full_name.partition("/")[2]


class LocalWorktreeRunner:
    """
    Implements `Runner`. `remote_url` maps `owner/repo` to a clone URL; tests point it at a local bare repo.

    With a `github` App, worktrees commit as the App's bot user (fact[bot]); otherwise, or if that lookup fails, as
    `commit_author_name`/`commit_author_email` from settings.

    `factory_root` is the checkout of the factory that is running; `prepare` refuses a workspace inside it or inside
    `pinned_dir`, so an agent never edits the code it runs on.
    """

    def __init__(
        self,
        settings: Settings,
        remote_url: Callable[[str], str] = github_url,
        github: GitHubApp | None = None,
        factory_root: Path = REPO_ROOT,
    ) -> None:
        self._settings = settings
        self._remote_url = remote_url
        self._github = github
        self._factory_root = factory_root
        self._mirror_locks: defaultdict[Path, asyncio.Lock] = defaultdict(asyncio.Lock)

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
        names = [_folder(r.full_name) for r in spec.repos]
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
        root = self._validate(spec)
        root.mkdir(parents=True, exist_ok=True)
        repos: dict[str, str] = {}
        try:
            for repo in spec.repos:
                worktree = root / _folder(repo.full_name)
                repos[repo.full_name] = str(worktree)
                await self._add_worktree(repo, worktree)
            (root / "WORKSPACE.md").write_text(_workspace_md(spec), encoding="utf-8", newline="\n")
        except BaseException:
            await self.teardown(WorkspaceHandle(run_id=spec.run_id, root=str(root), repos=repos))
            raise
        log.info("workspace_ready", run_id=spec.run_id, root=str(root), repos=list(repos))
        return WorkspaceHandle(run_id=spec.run_id, root=str(root), repos=repos)

    async def sync_mirror(self, full_name: str) -> Path:
        """Clone or fetch the bare mirror of `owner/repo` (remote branches under `origin/*`) and return its path."""
        mirror = self._mirror_path(full_name)
        async with self._mirror_locks[mirror]:
            if not (mirror / "HEAD").exists():
                mirror.parent.mkdir(parents=True, exist_ok=True)
                await self._git("clone", "--bare", self._remote_url(full_name), str(mirror), cwd=mirror.parent)
                # A bare clone maps no remote branches; keep them under origin/* so local branches are never overwritten.
                await self._git("config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*", cwd=mirror)
            await self._keep_bare_out_of_worktrees(mirror)
            await self._git("fetch", "--prune", "origin", cwd=mirror)
            await self._git_ok("remote", "set-head", "origin", "--auto", cwd=mirror)  # follow default-branch renames
        return mirror

    async def _keep_bare_out_of_worktrees(self, mirror: Path) -> None:
        """
        Keep `core.bare = true` in the mirror's own `config.worktree`, never in the shared config.

        With `extensions.worktreeConfig`, a shared `core.bare` also applies to every worktree, so git refuses
        work-tree commands there (`add`, `commit`). This also repairs mirrors cloned before the fix.
        """
        await self._git("config", "extensions.worktreeConfig", "true", cwd=mirror)  # before any --worktree write
        if await self._git_ok("config", "--file", "config", "--get", "core.bare", cwd=mirror):
            await self._git("config", "--worktree", "core.bare", "true", cwd=mirror)
            await self._git("config", "--file", "config", "--unset", "core.bare", cwd=mirror)

    async def _add_worktree(self, repo: RepoCheckout, worktree: Path) -> None:
        mirror = await self.sync_mirror(repo.full_name)
        remote_branch = f"refs/remotes/origin/{repo.branch}"
        resumed = await self._git_ok("rev-parse", "--verify", "--quiet", remote_branch, cwd=mirror)
        start = f"origin/{repo.branch}" if resumed else f"origin/{repo.default_branch}"
        await self._git("worktree", "prune", cwd=mirror)
        await self._git("worktree", "add", "--no-track", "-B", repo.branch, str(worktree), start, cwd=mirror)
        name, email = await self._commit_identity()
        await self._git("config", "--worktree", "user.name", name, cwd=worktree)
        await self._git("config", "--worktree", "user.email", email, cwd=worktree)
        log.info("worktree_added", repo=repo.full_name, branch=repo.branch, resumed=resumed)

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
        for full_name in handle.repos:
            try:
                mirror = self._mirror_path(full_name)
            except RunnerError:
                continue
            if mirror.exists():
                await self._git_ok("worktree", "remove", "--force", str(root / _folder(full_name)), cwd=mirror)
                await self._git_ok("worktree", "prune", cwd=mirror)
        if root.exists():
            shutil.rmtree(root, onexc=_make_writable_and_retry)
        log.info("workspace_removed", run_id=handle.run_id)

    async def _git_ok(self, *args: str, cwd: Path) -> bool:
        try:
            await self._git(*args, cwd=cwd)
        except RunnerError:
            return False
        return True

    async def _git(self, *args: str, cwd: Path) -> str:
        proc = await spawn(["git", *args], cwd, build_env({"GIT_TERMINAL_PROMPT": "0"}))
        lines = [item.text async for item in stream(proc, GIT_TIMEOUT) if isinstance(item, ExecLine)]
        out = "\n".join(lines)
        if proc.returncode != 0:
            log.warning("git_failed", args=list(args), exit_code=proc.returncode, output=out)
            msg = f"git {' '.join(args)} failed ({proc.returncode}): {out}"
            raise RunnerError(msg)
        return out


def _make_writable_and_retry(func: Callable[[str], object], path: str, _exc: BaseException) -> None:
    """Git marks object files read-only; on Windows `rmtree` needs them writable."""
    Path(path).chmod(stat.S_IWRITE)
    func(path)


def _workspace_md(spec: WorkspaceSpec) -> str:
    lines = [
        "# Workspace",
        "",
        f"Task: `{spec.task_key}`. Every repository below is checked out side by side on its own branch.",
        "Commit locally; never push.",
        "",
        "| Folder | Repository | Role | Branch |",
        "| --- | --- | --- | --- |",
    ]
    lines.extend(f"| `{_folder(r.full_name)}` | {r.full_name} | {r.role or '-'} | `{r.branch}` |" for r in spec.repos)
    return "\n".join(lines) + "\n"
