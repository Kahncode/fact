"""A worker serving a generation loads its recorded home SHAs; only the newest one makes its versions current."""

import json
import subprocess
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fact.core import generations
from fact.core.settings import ProjectRef, Settings
from fact.worker.generation import GenerationMismatchError, Refused, make_current, notify_refused, serve
from fact.worker.loader import factory_sha
from fact.worker.runners import LocalRunner

HELLO = """
from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from fact.sdk.workflow import FactWorkflow


@workflow.defn(name="hello")
class Hello(FactWorkflow):
    @workflow.run
    async def run(self, name: str) -> str:
        return "{text}"
"""


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)  # noqa: S603, S607
    return result.stdout.strip()


class Home:
    """A bare repository standing in for the project's home, with a clone to push commits from."""

    def __init__(self, root: Path) -> None:
        self.bare, self.seed = root / "home.git", root / "seed"
        _git("init", "--bare", "-b", "main", str(self.bare), cwd=root)
        _git("clone", str(self.bare), str(self.seed), cwd=root)
        _git("config", "user.name", "seed", cwd=self.seed)
        _git("config", "user.email", "seed@example.com", cwd=self.seed)

    def push(self, text: str) -> str:
        path = self.seed / ".fact" / "workflows" / "hello.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(HELLO.replace("{text}", text), encoding="utf-8")
        _git("add", ".", cwd=self.seed)
        _git("commit", "-m", text, cwd=self.seed)
        _git("push", "origin", "HEAD:main", cwd=self.seed)
        return _git("rev-parse", "HEAD", cwd=self.seed)


@pytest.fixture
def project() -> str:
    return f"gen-{uuid.uuid4().hex[:8]}"  # fresh module names per test


@pytest.fixture
def one_project(settings: Settings, project: str) -> Settings:
    return settings.model_copy(update={"projects": [ProjectRef(name=project, home="acme/home")]})


@pytest.fixture
def home(tmp_path: Path) -> Home:
    return Home(tmp_path)


@pytest.fixture
def runner(settings: Settings, home: Home) -> LocalRunner:
    return LocalRunner(settings, remote_url=lambda _name: home.bare.as_uri())


def _dir(settings: Settings) -> Path:
    return generations.generations_dir(settings.data_dir)


async def test_the_newest_generation_records_the_heads_it_loads(
    one_project: Settings, project: str, home: Home, runner: LocalRunner
) -> None:
    head = home.push("first")
    created = generations.create(_dir(one_project), await factory_sha())

    served = await serve(one_project, runner, created.id)

    assert served.current
    assert [p.home_sha for p in served.projects] == [head]
    assert generations.read(_dir(one_project), created.id).projects == {project: head}


async def test_an_older_generation_loads_its_recorded_sha_and_is_never_current(
    one_project: Settings, home: Home, runner: LocalRunner
) -> None:
    factory = await factory_sha()
    first = home.push("first")
    older = generations.create(_dir(one_project), factory)
    await serve(one_project, runner, older.id)
    home.push("second")  # the head moves on, and a newer generation is started
    generations.create(_dir(one_project), factory)

    served = await serve(one_project, runner, older.id)

    assert not served.current
    assert [p.home_sha for p in served.projects] == [first]
    client = MagicMock()
    client.workflow_service.set_worker_deployment_current_version = AsyncMock()
    await make_current(client, served)
    client.workflow_service.set_worker_deployment_current_version.assert_not_awaited()


async def test_an_older_generation_skips_a_project_it_never_recorded(
    one_project: Settings, home: Home, runner: LocalRunner
) -> None:
    home.push("first")
    factory = await factory_sha()
    older = generations.create(_dir(one_project), factory)  # its worker never recorded anything
    generations.create(_dir(one_project), factory)

    served = await serve(one_project, runner, older.id)

    assert served.projects == []
    assert generations.read(_dir(one_project), older.id).projects == {}


async def test_a_worker_refuses_another_pins_generation(one_project: Settings, runner: LocalRunner) -> None:
    other = generations.create(_dir(one_project), "0" * 40)

    with pytest.raises(GenerationMismatchError):
        await serve(one_project, runner, other.id)


async def test_a_refused_version_sends_one_error_notification_per_error() -> None:
    sent: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(201, json={"id": len(sent)})

    refused = [Refused("acme", "sandbox 'base' failed to build")]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        await notify_refused(http, "http://api.test", refused)
        await notify_refused(http, "http://api.test", refused)

    assert sent[0]["kind"] == "error"
    assert sent[0]["body"] == "sandbox 'base' failed to build"
    assert sent[0]["dedupe_key"] == sent[1]["dedupe_key"]  # the API keeps one per key: a restart repeats nothing
