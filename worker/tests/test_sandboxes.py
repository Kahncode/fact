"""Sandbox images: built once per folder hash when a version loads; a failed build or missing folder refuses it."""

import json
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from docker_env import needs_docker
from fact.core.settings import ProjectRef, Settings
from fact.worker import sandboxes
from fact.worker.generation import Refused, load_projects
from fact.worker.loader import load_project
from fact.worker.runners import LocalRunner
from fact.worker.sandboxes import LABEL_NAME, LABEL_PROJECT, LABEL_SANDBOX, DockerUnavailableError, image_tag

WORKFLOW = """
from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from fact.sdk.workflow import FactWorkflow


@workflow.defn(name="boxed")
class Boxed(FactWorkflow):
    sandbox = "tiny"

    @workflow.run
    async def run(self) -> None:
        return None


@workflow.defn(name="plain")
class Plain(FactWorkflow):
    @workflow.run
    async def run(self) -> None:
        return None
"""
CONFIG = """name: p
repositories: [{full_name: acme/home, role: app}]
workflows: {default: boxed}
runner_pool: {kind: docker, default_sandbox: base}
"""
SCRATCH = "FROM scratch\nCOPY marker.txt /marker.txt\n"  # builds offline, without pulling a base image


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)  # noqa: S603, S607
    return result.stdout.strip()


class Home:
    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.bare, self.seed = root / "home.git", root / "seed"
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


def _sandbox_files(names: tuple[str, ...] = ("tiny", "base"), dockerfile: str = SCRATCH) -> dict[str, str]:
    files = {".fact/workflows/boxed.py": WORKFLOW, ".fact/project.yaml": CONFIG}
    for name in names:
        files[f".fact/sandboxes/{name}/Dockerfile"] = dockerfile
        files[f".fact/sandboxes/{name}/marker.txt"] = f"{name}\n"
    return files


@pytest.fixture
def project() -> Iterator[str]:
    name = f"box-{uuid.uuid4().hex[:8]}"  # fresh module names and image tags per test
    yield name
    if shutil.which("docker"):
        images = subprocess.run(  # noqa: S603
            ["docker", "image", "ls", "--quiet", "--filter", f"label={LABEL_PROJECT}={name}"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        ).stdout.split()
        if images:
            rm = ["docker", "image", "rm", "--force", *images]
            subprocess.run(rm, capture_output=True, check=False)  # noqa: S603


@pytest.fixture
def home(tmp_path: Path) -> Home:
    return Home(tmp_path)


def _url(home: Home) -> str:
    return home.bare.as_uri()


def test_changing_a_file_changes_the_tag(tmp_path: Path) -> None:
    folder = tmp_path / "tiny"
    folder.mkdir()
    (folder / "Dockerfile").write_text(SCRATCH, encoding="utf-8")
    before = image_tag("Acme", "tiny", folder)
    (folder / "Dockerfile").write_text(SCRATCH + "# changed\n", encoding="utf-8")

    assert before.startswith("fact-sandbox/acme/tiny:")
    assert image_tag("Acme", "tiny", folder) != before


@needs_docker
async def test_builds_each_sandbox_once(
    settings: Settings, home: Home, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    home.push(_sandbox_files())
    built: list[str] = []
    real_build = sandboxes._build  # noqa: SLF001

    async def counting_build(project: str, name: str, folder: Path, tag: str) -> None:
        built.append(name)
        await real_build(project, name, folder, tag)

    monkeypatch.setattr(sandboxes, "_build", counting_build)

    loaded = await load_project(project, "acme/home", lambda _: _url(home), settings.pinned_dir, "f" * 40)
    again = await load_project(project, "acme/home", lambda _: _url(home), settings.pinned_dir, "f" * 40)

    assert built == ["base", "tiny"]  # `Plain` names none: the pool's default
    assert again.sandbox_images == loaded.sandbox_images
    tiny = loaded.sandbox_images["tiny"]
    assert tiny == image_tag(project, "tiny", loaded.checkout / ".fact" / "sandboxes" / "tiny")
    inspect = subprocess.run(  # noqa: S603, ASYNC221 - a test, after the loads
        ["docker", "image", "inspect", "--format", "{{json .Config.Labels}}", tiny],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    labels = json.loads(inspect.stdout)
    assert (labels[LABEL_SANDBOX], labels[LABEL_PROJECT], labels[LABEL_NAME]) == ("true", project, "tiny")


@needs_docker
async def test_a_failed_build_refuses_the_version_only(settings: Settings, tmp_path: Path, project: str) -> None:
    broken, fine = Home(tmp_path / "broken"), Home(tmp_path / "fine")
    broken.push(_sandbox_files(dockerfile="FROM scratch\nRUN false\n"))
    fine.push({".fact/workflows/boxed.py": WORKFLOW.replace('"boxed"', '"boxed2"').replace('"plain"', '"plain2"')})
    homes = {"acme/broken": broken, "acme/fine": fine}
    refs = [ProjectRef(name=project, home="acme/broken"), ProjectRef(name=f"{project}-ok", home="acme/fine")]
    runner = LocalRunner(settings, remote_url=lambda name: _url(homes[name]))
    refused: list[Refused] = []

    loaded = await load_projects(settings.model_copy(update={"projects": refs}), runner, "f" * 40, refused=refused)

    assert [p.project for p in loaded] == [f"{project}-ok"]
    assert [r.project for r in refused] == [project]
    assert "failed to build" in refused[0].error


async def test_a_missing_sandbox_folder_refuses_the_version(settings: Settings, home: Home, project: str) -> None:
    sha = home.push(_sandbox_files(names=("tiny",)))  # no `base`, the pool's default
    runner = LocalRunner(settings, remote_url=lambda _: _url(home))
    refused: list[Refused] = []
    settings = settings.model_copy(update={"projects": [ProjectRef(name=project, home="acme/home")]})

    assert await load_projects(settings, runner, "f" * 40, refused=refused) == []
    assert "sandbox 'base' has no folder .fact/sandboxes/base/" in refused[0].error
    assert (refused[0].home_sha, refused[0].sandbox) == (sha, "base")  # the refusal's dedupe key


async def test_docker_unavailable_stops_the_worker(
    settings: Settings, home: Home, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    home.push(_sandbox_files())
    monkeypatch.setattr(sandboxes, "DOCKER", "docker-not-installed-here")
    runner = LocalRunner(settings, remote_url=lambda _: _url(home))
    settings = settings.model_copy(update={"projects": [ProjectRef(name=project, home="acme/home")]})

    with pytest.raises(DockerUnavailableError, match="Docker is not available"):
        await load_projects(settings, runner, "f" * 40)
