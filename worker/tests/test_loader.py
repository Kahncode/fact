"""Loading a project's workflows from a temporary home repository, pinned to its default branch's SHA."""

import subprocess
import uuid
from pathlib import Path

import pytest
from fact.core.settings import REPO_ROOT, ProjectRef, Settings
from fact.sdk.workflow import FactWorkflow
from fact.worker.__main__ import DuplicateVersionError, load_projects
from fact.worker.loader import (
    WorkflowLoadError,
    factory_sha,
    home_sha_of,
    load_project,
    load_workflows,
    sandbox_runner,
    version_id,
)
from fact.worker.runners import LocalRunner
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

HOME = "acme/home"
HELLO = '''
from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from fact.sdk.workflow import FactWorkflow


class Helper:
    """Not a workflow: ignored."""


class Base(FactWorkflow):
    """A FactWorkflow subclass without @workflow.defn: ignored."""


@workflow.defn(name="hello")
class Hello(Base):
    @workflow.run
    async def run(self, name: str) -> str:
        return f"hello {name}"
'''


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)  # noqa: S603, S607
    return result.stdout.strip()


class HomeRepo:
    """A bare repository standing in for the project's home on GitHub, with a clone to push commits from."""

    def __init__(self, root: Path) -> None:
        self.bare = root / "home.git"
        self.seed = root / "seed"
        _git("init", "--bare", "-b", "main", str(self.bare), cwd=root)
        _git("clone", str(self.bare), str(self.seed), cwd=root)
        _git("config", "user.name", "seed", cwd=self.seed)
        _git("config", "user.email", "seed@example.com", cwd=self.seed)

    def push(self, files: dict[str, str]) -> str:
        for rel, text in files.items():
            path = self.seed / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        _git("add", ".", cwd=self.seed)
        _git("commit", "-m", "update", cwd=self.seed)
        _git("push", "origin", "HEAD:main", cwd=self.seed)
        return _git("rev-parse", "HEAD", cwd=self.seed)


@pytest.fixture
def home(tmp_path: Path) -> HomeRepo:
    return HomeRepo(tmp_path)


@pytest.fixture
def runner(settings: Settings, home: HomeRepo) -> LocalRunner:
    return LocalRunner(settings, remote_url=lambda _name: home.bare.as_uri())


def _project() -> str:
    """Return a fresh project name per test, so module names never collide across tests in one process."""
    return f"acme-{uuid.uuid4().hex[:8]}"


async def test_loads_workflows_pinned_to_the_default_branch(
    settings: Settings, home: HomeRepo, runner: LocalRunner
) -> None:
    sha = home.push({".fact/workflows/hello.py": HELLO, ".fact/workflows/_shared.py": "raise RuntimeError\n"})
    project = _project()

    loaded = await load_project(project, HOME, runner.remote_url, settings.pinned_dir, "f" * 40)

    assert loaded.home_sha == sha
    assert loaded.checkout == settings.pinned_dir / "projects" / project / sha
    assert _git("rev-parse", "HEAD", cwd=loaded.checkout) == sha
    assert [cls.__name__ for cls in loaded.workflows] == ["Hello"]
    assert issubclass(loaded.workflows[0], FactWorkflow)
    assert loaded.version == f"{'f' * 12}+{sha}"
    assert loaded.task_queue == loaded.deployment_name == f"wf-{project}"


async def test_reuses_the_checkout_and_follows_new_commits(
    settings: Settings, home: HomeRepo, runner: LocalRunner
) -> None:
    first_sha = home.push({".fact/workflows/hello.py": HELLO})
    project = _project()
    first = await load_project(project, HOME, runner.remote_url, settings.pinned_dir, "f" * 40)
    marker = first.checkout / "untracked.txt"
    marker.write_text("kept\n", encoding="utf-8")

    again = await load_project(project, HOME, runner.remote_url, settings.pinned_dir, "f" * 40)
    assert again.checkout == first.checkout
    assert marker.exists()  # reused, not re-created

    second_sha = home.push({"README.md": "new\n"})
    moved = await load_project(project, HOME, runner.remote_url, settings.pinned_dir, "f" * 40)
    assert moved.home_sha == second_sha != first_sha
    assert moved.checkout.name == second_sha
    assert first.checkout.exists()  # the old version's checkout stays for runs pinned to it


async def test_a_clone_cut_short_is_never_loaded(settings: Settings, home: HomeRepo, runner: LocalRunner) -> None:
    sha = home.push({".fact/workflows/hello.py": HELLO})
    project = _project()
    partial = settings.pinned_dir / "projects" / project / f".{sha}.clone"  # a worker killed mid-clone left this
    partial.mkdir(parents=True)
    (partial / "junk.txt").write_text("partial\n", encoding="utf-8")

    loaded = await load_project(project, HOME, runner.remote_url, settings.pinned_dir, "f" * 40)

    assert [cls.__name__ for cls in loaded.workflows] == ["Hello"]
    assert not partial.exists()


async def test_module_that_fails_to_import_names_the_file(
    settings: Settings, home: HomeRepo, runner: LocalRunner
) -> None:
    home.push({".fact/workflows/broken.py": "import does_not_exist_anywhere\n"})

    with pytest.raises(WorkflowLoadError, match=r"broken\.py: ModuleNotFoundError") as info:
        await load_project(_project(), HOME, runner.remote_url, settings.pinned_dir, "f" * 40)
    assert "does_not_exist_anywhere" in str(info.value)


async def test_invalid_project_name_is_refused(settings: Settings, runner: LocalRunner) -> None:
    with pytest.raises(WorkflowLoadError, match="invalid project name"):
        await load_project("../escape", HOME, runner.remote_url, settings.pinned_dir, "f" * 40)


async def test_loaded_workflow_runs_in_the_sandbox(settings: Settings, home: HomeRepo, runner: LocalRunner) -> None:
    home.push({".fact/workflows/hello.py": HELLO})
    loaded = await load_project(_project(), HOME, runner.remote_url, settings.pinned_dir, "f" * 40)
    env_cm = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    async with (
        env_cm as env,
        Worker(
            env.client, task_queue=loaded.task_queue, workflows=list(loaded.workflows), workflow_runner=sandbox_runner()
        ),
    ):
        result = await env.client.execute_workflow(
            "hello", "fact", id=f"hello-{loaded.project}", task_queue=loaded.task_queue, result_type=str
        )
    assert result == "hello fact"


def test_version_id_format() -> None:
    factory, home_sha = "a" * 40, "b" * 40
    version = version_id(factory, home_sha)
    assert version == f"{'a' * 12}+{home_sha}"
    assert len(version) <= 64  # noqa: PLR2004 - the `worker_version` column
    assert home_sha_of(version) == home_sha
    assert home_sha_of("not-a-version") is None


async def test_factory_sha_is_the_checkout_head(tmp_path: Path) -> None:
    assert await factory_sha(REPO_ROOT) == _git("rev-parse", "HEAD", cwd=REPO_ROOT)
    assert await factory_sha(tmp_path) == "unknown"


async def test_load_projects_skips_a_project_that_fails(
    settings: Settings, home: HomeRepo, runner: LocalRunner
) -> None:
    home.push({".fact/workflows/broken.py": "raise RuntimeError('boom')\n"})  # every project's home is this repo

    assert await load_projects(settings, runner) == []


def test_files_that_map_to_one_module_name_are_refused(tmp_path: Path) -> None:
    workflows_dir = tmp_path / ".fact" / "workflows"
    workflows_dir.mkdir(parents=True)
    (workflows_dir / "a-b.py").write_text("", encoding="utf-8")
    (workflows_dir / "a_b.py").write_text("", encoding="utf-8")

    with pytest.raises(WorkflowLoadError, match=r"a_b\.py: its module name .* is taken by .*a-b\.py"):
        load_workflows(_project(), tmp_path, "e" * 40)


async def test_load_projects_refuses_two_projects_with_one_build_id(
    settings: Settings, home: HomeRepo, runner: LocalRunner
) -> None:
    home.push({".fact/workflows/hello.py": HELLO})
    twins = [ProjectRef(name=_project(), home=HOME), ProjectRef(name=_project(), home=HOME)]

    with pytest.raises(DuplicateVersionError, match="share build id"):
        await load_projects(settings.model_copy(update={"projects": twins}), runner)
