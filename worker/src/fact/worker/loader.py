"""
Load each project's workflows from its home repository, pinned to one SHA.

For a project: ask the home repository for its default branch's SHA (`git ls-remote`), clone it and check that SHA
out detached at `pinned_dir/projects/<project>/<sha>` (reused when it is already there), and import
`.fact/workflows/*.py` under module names unique to the project and SHA. The modules are found by a meta path finder,
so the workflow sandbox can import them again by name.
"""

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import inspect
import re
import shutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from fact.core.activity_contract import workflow_queue
from fact.core.generations import SHORT_SHA, version_id
from fact.core.logging import get_logger
from fact.core.settings import REPO_ROOT
from fact.sdk.workflow import FactWorkflow
from fact.worker.git_push import run_git
from fact.worker.runners.workspace_files import make_writable_and_retry
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

log = get_logger(__name__)

WORKFLOWS_SUBDIR = Path(".fact") / "workflows"
UNKNOWN_FACTORY = "unknown"  # the factory is not a git checkout
_PROJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
# Imported outside the sandbox and shared with it: the SDK (and the core types it uses), pydantic, rich (logging).
SANDBOX_PASSTHROUGH = ("fact", "pydantic", "pydantic_core", "rich")

RemoteUrl = Callable[[str], str]  # `owner/repo` to its clone URL


class WorkflowLoadError(Exception):
    """A project's workflows could not be checked out or imported; the message names the project or file."""


@dataclass(frozen=True)
class ProjectWorkflows:
    project: str
    home: str  # owner/repo
    home_sha: str
    checkout: Path
    version: str  # `<factory SHA>+<home SHA>`: the worker deployment build id
    workflows: tuple[type[FactWorkflow], ...]

    @property
    def task_queue(self) -> str:
        return workflow_queue(self.project)

    @property
    def deployment_name(self) -> str:
        return self.task_queue


def home_sha_of(version: str) -> str | None:
    """Return the home SHA part of a version id, or None if `version` is not one."""
    _, plus, home = version.partition("+")
    return home if plus and home else None


def sandbox_runner() -> SandboxedWorkflowRunner:
    return SandboxedWorkflowRunner(
        restrictions=SandboxRestrictions.default.with_passthrough_modules(*SANDBOX_PASSTHROUGH)
    )


async def _git(args: list[str], cwd: Path) -> str:
    code, lines = await run_git(args, cwd)
    if code != 0:
        msg = f"git {' '.join(args)} failed ({code}) in {cwd}: {' '.join(lines)}"
        raise WorkflowLoadError(msg)
    return "\n".join(lines).strip()


async def factory_sha(root: Path = REPO_ROOT) -> str:
    """Return the factory checkout's HEAD SHA, or `unknown` when it is not a git checkout."""
    code, lines = await run_git(["rev-parse", "--verify", "HEAD"], root)
    sha = lines[0].strip() if code == 0 and lines else ""
    if not _SHA.match(sha):
        log.warning("factory_sha_unknown", root=str(root))
        return UNKNOWN_FACTORY
    return sha


async def default_branch_sha(url: str, cwd: Path) -> str:
    """Return the SHA the remote's `HEAD` (its default branch) points at."""
    for line in (await _git(["ls-remote", url, "HEAD"], cwd)).splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == "HEAD" and _SHA.match(sha):
            return sha
    msg = f"{url} has no default branch (empty repository?)"
    raise WorkflowLoadError(msg)


async def pinned_checkout(url: str, pinned_dir: Path, project: str, sha: str) -> Path:
    """Create, or reuse, a clone of `url` detached at `sha`, at `pinned_dir/projects/<project>/<sha>`."""
    path = pinned_dir / "projects" / project / sha
    if (path / ".git").exists():
        code, lines = await run_git(["rev-parse", "HEAD"], path)
        if code == 0 and lines and lines[0].strip() == sha:
            return path
        log.warning("pinned_checkout_replaced", project=project, path=str(path))
    partial = path.with_name(f".{sha}.clone")  # a leading dot: never a SHA, so a clone cut short is never taken
    for stale in (partial, path):
        if stale.exists():
            shutil.rmtree(stale, onexc=make_writable_and_retry)
    path.parent.mkdir(parents=True, exist_ok=True)
    await _git(["clone", "--no-checkout", url, str(partial)], path.parent)
    await _git(["checkout", "--detach", sha], partial)
    partial.rename(path)
    return path


class _WorkflowFinder(importlib.abc.MetaPathFinder):
    """Finds the loaded workflow modules by their unique names, in the host and inside the workflow sandbox."""

    def __init__(self) -> None:
        self.files: dict[str, Path] = {}

    def find_spec(
        self, fullname: str, path: Sequence[str] | None = None, target: ModuleType | None = None  # noqa: ARG002
    ) -> importlib.machinery.ModuleSpec | None:
        file = self.files.get(fullname)
        return importlib.util.spec_from_file_location(fullname, file) if file else None


_FINDER = _WorkflowFinder()


def _ident(text: str) -> str:
    return re.sub(r"\W", "_", text)


def module_name(project: str, sha: str, file: Path) -> str:
    return f"fact_workflows_{_ident(project)}_{sha[:SHORT_SHA]}_{_ident(file.stem)}"


def _import(name: str, file: Path) -> ModuleType:
    if _FINDER not in sys.meta_path:
        sys.meta_path.append(_FINDER)
    if _FINDER.files.get(name, file) != file:
        msg = f"{file}: its module name {name} is taken by {_FINDER.files[name]}; rename one of them"
        raise WorkflowLoadError(msg)
    _FINDER.files[name] = file
    try:
        return importlib.import_module(name)
    except Exception as exc:
        sys.modules.pop(name, None)
        del _FINDER.files[name]
        msg = f"{file}: {type(exc).__name__}: {exc}"
        raise WorkflowLoadError(msg) from exc


def _workflow_classes(module: ModuleType) -> list[type[FactWorkflow]]:
    """`FactWorkflow` subclasses defined in `module` and decorated with `@workflow.defn` themselves."""
    return [
        cls
        for _, cls in inspect.getmembers(module, inspect.isclass)
        if issubclass(cls, FactWorkflow)
        and cls.__module__ == module.__name__
        and "__temporal_workflow_definition" in vars(cls)
    ]


def load_workflows(project: str, checkout: Path, sha: str) -> tuple[type[FactWorkflow], ...]:
    """Import every `.fact/workflows/*.py` of the checkout (sorted, `_`-prefixed files skipped)."""
    found: list[type[FactWorkflow]] = []
    for file in sorted((checkout / WORKFLOWS_SUBDIR).glob("*.py")):
        if file.name.startswith("_"):
            continue
        found.extend(_workflow_classes(_import(module_name(project, sha, file), file)))
    return tuple(found)


async def load_project(  # noqa: PLR0913
    project: str, home: str, remote_url: RemoteUrl, pinned_dir: Path, factory: str, *, sha: str | None = None
) -> ProjectWorkflows:
    """
    Load `project`'s workflows from a checkout of its home repository at `sha`.

    Without `sha`, the home repository's default-branch SHA now (a generation's first start, or no generation).
    """
    if not _PROJECT.match(project):
        msg = f"invalid project name {project!r}"
        raise WorkflowLoadError(msg)
    if sha is not None and not _SHA.match(sha):
        msg = f"invalid home SHA {sha!r} for project {project!r}"
        raise WorkflowLoadError(msg)
    url = remote_url(home)
    pinned_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - one directory, instant
    if sha is None:
        sha = await default_branch_sha(url, pinned_dir)
    checkout = await pinned_checkout(url, pinned_dir, project, sha)
    workflows = load_workflows(project, checkout, sha)
    names = [cls.__name__ for cls in workflows]
    log.info("project_workflows_loaded", project=project, home_sha=sha, workflows=names, checkout=str(checkout))
    return ProjectWorkflows(project, home, sha, checkout, version_id(factory, sha), workflows)
