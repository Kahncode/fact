"""
`classify_signals`: the deterministic half of classify. Raises review signals from the change set, with no LLM.

- `schema_change`, `api_change`, `always_review`: a changed path matches a glob of the project's `classify` settings.
- `api_breaking`: a JSON file under `api_paths` (such as the OpenAPI snapshot) lost or changed anything; additions alone
  are only `api_change`.
- `task_closed`: the task's source says the task was closed while the run went on.

The globs and the task source come from the project's registered checkout, not the run's branch, so a change cannot
loosen the rules it is judged by. Each repository's diff is its branch against the merge base with its default branch.
"""

import asyncio
import json
from pathlib import Path, PurePosixPath
from typing import Any

from fact.core.activity_contract import CLASSIFY_SIGNALS, ClassifySignalsInput
from fact.core.classification import ChangedPath, ClassifySignal, SignalName
from fact.core.github import GitHubAppConfigError
from fact.core.logging import bind_run, get_logger
from fact.core.project_config import ConfigError, ProjectConfig, load_project_config
from fact.core.settings import Settings, get_settings, project_path
from fact.core.tasks import source_for
from fact.worker.git_push import run_git
from temporalio import activity
from temporalio.exceptions import ApplicationError

log = get_logger(__name__)

PATH_SIGNALS: tuple[tuple[SignalName, str], ...] = (
    ("schema_change", "schema_paths"),
    ("api_change", "api_paths"),
    ("always_review", "always_review_paths"),
)


def _non_retryable(message: str) -> ApplicationError:
    return ApplicationError(message, non_retryable=True)


def matches(path: str, globs: list[str]) -> bool:
    """Whether a repository-relative path matches any glob; `**` spans folders, `*` stays within one."""
    pure = PurePosixPath(path)
    return any(pure.full_match(glob) for glob in globs)


def removes_or_changes(old: Any, new: Any) -> bool:  # noqa: ANN401 - any JSON value
    """
    Whether `new` lost or changed anything `old` had; additions alone are not.

    Objects keep every key; arrays keep every element (in any order, so an edited element counts as removed);
    scalars stay equal.
    """
    if isinstance(old, dict):
        return not isinstance(new, dict) or any(k not in new or removes_or_changes(v, new[k]) for k, v in old.items())
    if isinstance(old, list):
        return not isinstance(new, list) or any(item not in new for item in old)
    return old != new


async def _git(args: list[str], worktree: Path) -> tuple[int, list[str]]:
    return await run_git(["-c", "core.quotePath=false", *args], worktree)


async def _changed(worktree: Path, default_branch: str) -> tuple[str, list[str]]:
    """Return the merge base with `origin/<default_branch>` and the paths changed since it, up to HEAD."""
    code, lines = await _git(["merge-base", "HEAD", f"origin/{default_branch}"], worktree)
    if code != 0 or not lines:
        msg = f"no merge base with origin/{default_branch} in {worktree}: {' '.join(lines)}"
        raise _non_retryable(msg)
    base = lines[0].strip()
    code, lines = await _git(["diff", "--name-only", "--no-renames", base, "HEAD"], worktree)
    if code != 0:
        msg = f"git diff failed in {worktree} ({code}): {' '.join(lines)}"
        raise _non_retryable(msg)
    return base, [line.strip() for line in lines if line.strip()]


async def _json_at(worktree: Path, rev: str, path: str) -> Any:  # noqa: ANN401 - any JSON value
    """Read the JSON file at `rev:path`; None when it is missing or is not JSON."""
    code, lines = await _git(["show", f"{rev}:{path}"], worktree)
    if code != 0:
        return None
    try:
        return json.loads("\n".join(lines))
    except ValueError:
        return None


async def _api_breaking(worktree: Path, base: str, path: str) -> bool:
    old = await _json_at(worktree, base, path)
    if old is None:  # new file, or not JSON: nothing published to break
        return False
    return removes_or_changes(old, await _json_at(worktree, "HEAD", path))


async def _repo_signals(config: ProjectConfig, repo: str, worktree: Path) -> dict[SignalName, list[ChangedPath]]:
    default_branch = next((r.default_branch for r in config.repositories if r.full_name == repo), "main")
    base, paths = await _changed(worktree, default_branch)
    found: dict[SignalName, list[ChangedPath]] = {}
    for path in paths:
        changed = ChangedPath(repo=repo, path=path)
        for name, field in PATH_SIGNALS:
            if matches(path, getattr(config.classify, field)):
                found.setdefault(name, []).append(changed)
        if matches(path, config.classify.api_paths) and await _api_breaking(worktree, base, path):
            found.setdefault("api_breaking", []).append(changed)
    return found


class ClassifyActivities:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings

    def _project(self, name: str) -> tuple[ProjectConfig, Path]:
        settings = self._settings or get_settings()
        ref = next((r for r in settings.projects if r.name == name), None)
        path = project_path(ref) if ref else None
        if path is None:
            msg = f"project {name!r} has no registered checkout to read its classify settings from"
            raise _non_retryable(msg)
        try:
            return load_project_config(path), path
        except ConfigError as exc:
            raise _non_retryable(str(exc)) from exc

    async def _task_closed(self, config: ProjectConfig, root: Path, key: str) -> bool:
        try:
            source = source_for(config, self._settings or get_settings(), root)
        except GitHubAppConfigError as exc:
            log.warning("task_source_unavailable", task_key=key, error=str(exc))
            return False
        return await source.is_closed(key)

    @activity.defn(name=CLASSIFY_SIGNALS)
    async def classify_signals(self, spec: ClassifySignalsInput) -> list[ClassifySignal]:
        with bind_run(spec.run_id):
            config, root = await asyncio.to_thread(self._project, spec.project)
            found: dict[SignalName, list[ChangedPath]] = {}
            for repo, worktree in spec.handle.repos.items():
                for name, paths in (await _repo_signals(config, repo, Path(worktree))).items():
                    found.setdefault(name, []).extend(paths)
            if await self._task_closed(config, root, spec.task_key):
                found["task_closed"] = []
            signals = [ClassifySignal(name=name, paths=paths) for name, paths in found.items()]
            log.info("classify_signals", signals={s.name: [p.path for p in s.paths] for s in signals})
            return signals
