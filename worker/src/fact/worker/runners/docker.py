"""
Docker runner: one hardened container per run, the workspace on a Docker volume, every repository cloned inside.

No command of a run touches the host's disk: the worker drives the container through the `docker` CLI
(`docker_cli.py`), and `WorkspaceHandle` paths are container paths (`/workspace/<repo name>`).

GitHub access inside is a `contents: read` installation token for the run's repositories, minted (or reused while
fresh) for each command, prepare's network git commands included, and handed to it in its environment as
`GITHUB_TOKEN`. Git reads it through a credential helper set in the container user's global git config, which holds
the helper, never the token.
"""

import posixpath
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath

from fact.core.activity_contract import SANDBOX_QUEUE
from fact.core.github import GitHubApp
from fact.core.logging import get_logger
from fact.core.project_config import RunnerPoolConfig
from fact.core.settings import Settings
from fact.core.types import RepoCheckout, WorkspaceHandle, WorkspaceSpec
from fact.worker.runners import docker_cli
from fact.worker.runners.base import ExecLine, ExecOutput, RunnerCapabilities, RunnerError
from fact.worker.runners.docker_cli import WORKSPACE, container_name
from fact.worker.runners.local import check_repos, check_run_id, commit_identity, github_url
from fact.worker.runners.workspace_files import WORKSPACE_MD, folder, run_lock, workspace_md

log = get_logger(__name__)

PROFILE = "docker"
GIT_TIMEOUT = 600.0
TOKEN_ENV = "GITHUB_TOKEN"  # noqa: S105 - the variable's name, not a token
READ_PERMISSIONS = {"contents": "read"}
_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0"}
_CREDENTIAL_HELPER = f'!f() {{ test "$1" = get && echo username=x-access-token && echo "password=${TOKEN_ENV}"; }}; f'
_WRITE_FILE = 'printf %s "$FACT_FILE_TEXT" > "$0" && mv "$0" "$1"'


class DockerRunner:
    """
    Implements `Runner` for a project version whose `runner_pool.kind` is `docker`.

    `images` maps each sandbox name of that version to its image tag (`sandboxes.py`); `WorkspaceSpec.sandbox` picks
    one. `remote_url` maps `owner/repo` to the URL cloned inside the container; tests point it at a bare repository
    inside their image. Without a `github` App, commands get no token.

    It keeps no coordination state in memory: a run's `prepare` and `teardown` take turns on the same lock file as
    the local runner's, beside where its folder would be (nothing of the run is written there).
    """

    def __init__(  # noqa: PLR0913 - options are keyword-only
        self,
        settings: Settings,
        images: Mapping[str, str],
        pool: RunnerPoolConfig,
        *,
        remote_url: Callable[[str], str] = github_url,
        github: GitHubApp | None = None,
        task_queue: str = SANDBOX_QUEUE,
    ) -> None:
        self._settings = settings
        self._images = dict(images)
        self._pool = pool
        self.remote_url = remote_url
        self._github = github
        self.task_queue = task_queue

    def capabilities(self) -> RunnerCapabilities:
        return RunnerCapabilities(name=PROFILE, isolation="container", runs_as_user=False, supports_resume=True)

    def _image(self, spec: WorkspaceSpec) -> str:
        tag = self._images.get(spec.sandbox or "")
        if tag is None:
            msg = f"no image for sandbox {spec.sandbox!r}; this version has {sorted(self._images)}"
            raise RunnerError(msg)
        return tag

    def _lock_path(self, run_id: str) -> Path:
        check_run_id(run_id)
        return self._settings.workspaces_dir / run_id

    async def prepare(self, spec: WorkspaceSpec) -> WorkspaceHandle:
        """Create the run's container and clones, or return those an earlier attempt of this run finished."""
        check_repos(spec)
        image = self._image(spec)
        async with run_lock(self._lock_path(spec.run_id)):
            handle = await self._finished_workspace(spec)
            if handle is not None:
                log.info("workspace_reused", run_id=spec.run_id, branches=handle.branches)
                return handle
            return await self._create(spec, image)

    async def _finished_workspace(self, spec: WorkspaceSpec) -> WorkspaceHandle | None:
        """Read back the handle of a workspace an earlier attempt finished (`WORKSPACE.md` written); None otherwise."""
        container = container_name(spec.run_id)
        code, _ = await docker_cli.docker(["container", "inspect", "--format", "{{.Id}}", container])
        if code != 0:
            return None
        try:
            await self._run(spec.run_id, ["test", "-f", str(WORKSPACE / WORKSPACE_MD)], WORKSPACE, {})
            branches = {
                repo.full_name: await self._git(spec.run_id, ["branch", "--show-current"], _clone(repo.full_name), {})
                for repo in spec.repos
            }
        except RunnerError:
            return None
        if not all(branches.values()):
            return None
        return self._handle(spec, branches)

    def _handle(self, spec: WorkspaceSpec, branches: dict[str, str]) -> WorkspaceHandle:
        repos = {repo.full_name: str(_clone(repo.full_name)) for repo in spec.repos}
        return WorkspaceHandle(
            run_id=spec.run_id, root=str(WORKSPACE), task_queue=self.task_queue, repos=repos, branches=branches
        )

    async def _create(self, spec: WorkspaceSpec, image: str) -> WorkspaceHandle:
        await docker_cli.remove(spec.run_id)  # a retry after a crash mid-prepare finds this run's half-made one
        try:
            volume = docker_cli.volume_name(spec.run_id)
            await docker_cli.docker_ok(["volume", "create", "--label", f"{docker_cli.LABEL_RUN}={spec.run_id}", volume])
            await docker_cli.docker_ok(docker_cli.run_args(spec.run_id, image, self._pool, spec.sandbox_args))
            helper = ["config", "--global", "credential.https://github.com.helper", _CREDENTIAL_HELPER]
            await self._git(spec.run_id, helper, WORKSPACE, {})
            full_names = [repo.full_name for repo in spec.repos]
            branches = {repo.full_name: await self._clone(spec.run_id, repo, full_names) for repo in spec.repos}
            await self._write(spec.run_id, WORKSPACE / WORKSPACE_MD, workspace_md(spec, branches))
        except BaseException:
            await docker_cli.remove(spec.run_id)
            raise
        log.info("workspace_ready", run_id=spec.run_id, image=image, repos=[r.full_name for r in spec.repos])
        return self._handle(spec, branches)

    async def _clone(self, run_id: str, repo: RepoCheckout, full_names: Sequence[str]) -> str:
        """
        Clone in full (LFS included) and check out the task branch: from `origin/<branch>` if pushed, else new.

        Each network command (the clone, the checkout that downloads LFS objects) gets its own fresh token, so a long
        prepare never outlives the one it started with.
        """
        clone = _clone(repo.full_name)
        url = self.remote_url(repo.full_name)
        await self._git(
            run_id, ["clone", "--no-checkout", url, str(clone)], WORKSPACE, await self._token_env(full_names)
        )
        await self._git(run_id, ["lfs", "install", "--local"], clone, {})
        remote_branch = f"refs/remotes/origin/{repo.branch}"
        try:
            await self._git(run_id, ["rev-parse", "--verify", "--quiet", remote_branch], clone, {})
            resumed = True
        except RunnerError:
            resumed = False
        start = f"origin/{repo.branch}" if resumed else f"origin/{repo.default_branch}"
        checkout = ["checkout", "--no-track", "-B", repo.branch, start]
        await self._git(run_id, checkout, clone, await self._token_env(full_names))  # LFS smudges here
        name, email = await commit_identity(self._github, self._settings)
        await self._git(run_id, ["config", "user.name", name], clone, {})
        await self._git(run_id, ["config", "user.email", email], clone, {})
        log.info("repo_cloned", repo=repo.full_name, branch=repo.branch, resumed=resumed)
        return repo.branch

    async def _write(self, run_id: str, path: PurePosixPath, text: str) -> None:
        """Write a file in the container and rename it into place, so a half-written one never counts."""
        partial = path.with_name(f".{path.name}.partial")
        await self._run(run_id, ["sh", "-c", _WRITE_FILE, str(partial), str(path)], WORKSPACE, {"FACT_FILE_TEXT": text})

    async def _token_env(self, full_names: Sequence[str]) -> dict[str, str]:
        if self._github is None or not full_names:
            return {}
        token = await self._github.repositories_token(full_names, READ_PERMISSIONS)
        return {TOKEN_ENV: token.token}

    async def _run(self, run_id: str, argv: Sequence[str], cwd: PurePosixPath, env: Mapping[str, str]) -> str:
        """Run a command of fact's own; its output, or `RunnerError` with it unless it exits 0."""
        lines: list[str] = []
        code = -1
        async for item in self._exec(run_id, argv, cwd, env, GIT_TIMEOUT):
            if isinstance(item, ExecLine):
                lines.append(item.text)
            else:
                code = item.exit_code
        out = "\n".join(lines)
        if code != 0:
            log.warning("container_command_failed", run_id=run_id, argv=list(argv[:2]), exit_code=code, output=out)
            msg = f"{' '.join(argv[:2])} failed ({code}) in {container_name(run_id)}: {out}"
            raise RunnerError(msg)
        return out

    async def _git(self, run_id: str, args: Sequence[str], cwd: PurePosixPath, env: Mapping[str, str]) -> str:
        return (await self._run(run_id, ["git", *args], cwd, {**_GIT_ENV, **env})).strip()

    async def fetch(self, handle: WorkspaceHandle, full_name: str) -> None:
        """Fetch the container's clone of `full_name`, so its `origin/*` branches are current."""
        clone = _workdir(handle.repos[full_name])
        env = await self._token_env(list(handle.repos))
        await self._git(handle.run_id, ["fetch", "--prune", "origin"], clone, env)

    def exec(
        self,
        handle: WorkspaceHandle,
        argv: Sequence[str],
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[ExecOutput]:
        """Run `argv` in the run's container, in `cwd` under `/workspace`, with `env` plus a fresh read token."""
        check_run_id(handle.run_id)
        if not argv:
            msg = "argv is empty"
            raise RunnerError(msg)
        return self._exec_with_token(handle, argv, _workdir(cwd), dict(env or {}), timeout)

    async def _exec_with_token(
        self,
        handle: WorkspaceHandle,
        argv: Sequence[str],
        cwd: PurePosixPath,
        env: dict[str, str],
        timeout: float | None,  # noqa: ASYNC109 - enforced by `stream`
    ) -> AsyncIterator[ExecOutput]:
        token = await self._token_env(list(handle.repos))
        async for item in self._exec(handle.run_id, argv, cwd, {**token, **env}, timeout):  # the caller's env wins
            yield item

    async def _exec(
        self,
        run_id: str,
        argv: Sequence[str],
        cwd: PurePosixPath,
        env: Mapping[str, str],
        timeout: float | None,  # noqa: ASYNC109 - enforced by `stream`
    ) -> AsyncIterator[ExecOutput]:
        container = container_name(run_id)
        await docker_cli.ensure_running(container)
        async for item in docker_cli.exec_stream(container, argv, cwd, env, timeout):
            yield item

    async def teardown(self, handle: WorkspaceHandle) -> None:
        async with run_lock(self._lock_path(handle.run_id)):
            await docker_cli.remove(handle.run_id)
        log.info("workspace_removed", run_id=handle.run_id)


def _clone(full_name: str) -> PurePosixPath:
    return WORKSPACE / folder(full_name)


def _workdir(cwd: Path | str | None) -> PurePosixPath:
    """Resolve `cwd` (relative to `/workspace`, or a container path under it); `RunnerError` when it leaves it."""
    if cwd is None or str(cwd) in {"", "."}:
        return WORKSPACE
    raw = cwd.as_posix() if isinstance(cwd, Path) else str(cwd).replace("\\", "/")
    resolved = PurePosixPath(posixpath.normpath(posixpath.join(str(WORKSPACE), raw)))
    if not resolved.is_relative_to(WORKSPACE):
        msg = f"cwd {str(cwd)!r} is outside the workspace"
        raise RunnerError(msg)
    return resolved
