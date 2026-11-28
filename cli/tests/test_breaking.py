"""A breaking migration makes the follower stop the open runs, upgrade, and start their tasks again on the new pin."""

import json
from pathlib import Path

import httpx
import pytest
from fact.cli import follow, projects
from fact.cli.breaking import CHECK_INTERVAL_S, REASON, BreakingUpgrade, read_state
from fact.cli.follow import Follower
from fact.cli.generations import Generations
from fact.cli.processes import ChildSpec, RunDir
from fact.cli.supervisor import Changes
from fact.cli.upgrade_api import HttpUpgradeApi, OpenRun
from fact.cli.use import current_pin
from fact.core import generations
from fact.core.migration_rules import VERSIONS
from fact.core.settings import ProjectRef, Settings
from follow_fakes import NEW, OLD, Clock, Remote, make_workers, started

RUNS = [OpenRun("r1", "gh:o/acme#1", "acme"), OpenRun("r2", "gh:o/acme#2", "acme")]


class FakeApi:
    """The API as the upgrade sees it: two open runs, which end `stopped` once `finish` says so."""

    def __init__(self, *, scheduler: bool = True) -> None:
        self.scheduler, self.stopping, self.done, self.started = scheduler, list[str](), set[str](), list[str]()

    def scheduler_on(self) -> bool:
        return self.scheduler

    def set_scheduler(self, *, on: bool) -> None:
        self.scheduler = on

    def open_runs(self, project_names: list[str]) -> list[OpenRun]:
        assert project_names == ["acme"]
        assert not self.scheduler  # off first: no run starts while they are listed
        return RUNS

    def stop(self, run_id: str, reason: str) -> None:
        assert reason == REASON
        self.stopping.append(run_id)

    def finished(self, run_id: str) -> bool:
        return run_id in self.done

    def finish(self) -> None:
        self.done |= set(self.stopping)

    def start(self, task_key: str, project: str) -> str | None:
        assert project == "acme"
        self.started.append(task_key)
        return None


def _migrations(checkout: Path, *revisions: tuple[str, str | None, str]) -> None:
    folder = checkout / VERSIONS
    folder.mkdir(parents=True, exist_ok=True)
    for rev, down, body in revisions:
        source = f"revision = {rev!r}\ndown_revision = {down!r}\n"
        if "drop" in body:
            source += "breaking = True\n"
        (folder / f"{rev}.py").write_text(f"{source}\ndef upgrade():\n    {body}\n", encoding="utf-8")


@pytest.fixture
def project_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"projects": [ProjectRef(name="acme", home="o/acme")]})


@pytest.fixture
def workers(project_settings: Settings, tmp_path: Path) -> Generations:
    manager = make_workers(project_settings, tmp_path / "run")
    _migrations(current_pin(project_settings), ("m1", None, 'op.create_table("a")'))
    return manager


class MigratingRemote(Remote):
    """`main`'s new pin adds migration `m2`: `migration` is its `upgrade()` (breaking when it drops)."""

    migration = 'op.drop_column("a", "b")'

    def prepare(self, *args: object) -> Path:
        checkout = super().prepare(*args)
        _migrations(checkout, ("m1", None, 'op.create_table("a")'), ("m2", "m1", self.migration))
        return checkout


@pytest.fixture
def remote(project_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> MigratingRemote:
    fake = MigratingRemote(project_settings)
    monkeypatch.setattr(follow, "latest", fake.latest)
    monkeypatch.setattr(follow, "prepare", fake.prepare)
    return fake


class Factory:
    """The follower and the breaking upgrade, polled as `fact dev` does; `restart` is a new `fact dev`."""

    def __init__(self, settings: Settings, workers: Generations, remote: MigratingRemote, api: FakeApi) -> None:
        self.settings, self.workers, self.remote, self.api, self.clock = settings, workers, remote, api, Clock()
        self.restart()

    def restart(self) -> None:
        self.breaking = BreakingUpgrade(self.settings, workers=self.workers, log=print, api=self.api, clock=self.clock)
        self.follower = Follower(
            self.settings,
            "main",
            uv="uv",
            workers=self.workers,
            breaking=self.breaking,
            log=print,
            clock=self.clock,
            resolve_home=lambda _home: "c" * 40,
        )

    def tick(self) -> Changes | None:
        self.clock.now += follow.CHECK_INTERVAL_S + CHECK_INTERVAL_S
        found = [c for c in (self.follower(), self.breaking()) if c]
        assert len(found) <= 1
        return found[0] if found else None


def _factory(settings: Settings, workers: Generations, remote: MigratingRemote, api: FakeApi) -> Factory:
    factory = Factory(settings, workers, remote, api)
    remote.main = NEW
    assert factory.tick() is None  # breaking: the new pin is prepared, not current
    assert current_pin(settings).name == OLD
    return factory


def _old_worker(workers: Generations) -> str:
    return f"worker-{generations.all_generations(workers.directory)[0].id}"


def test_a_breaking_upgrade_stops_the_runs_upgrades_and_starts_their_tasks_again(
    project_settings: Settings, workers: Generations, remote: MigratingRemote
) -> None:
    api = FakeApi()
    factory = _factory(project_settings, workers, remote, api)
    old_worker = _old_worker(workers)

    assert factory.tick() is None
    assert (api.scheduler, api.stopping) == (False, ["r1", "r2"])
    assert factory.tick() is None  # the runs write their handoff notes on their own generation
    assert current_pin(project_settings).name == OLD

    api.finish()
    changes = factory.tick()
    newest = generations.newest(workers.directory)
    assert newest is not None
    assert newest.factory == NEW
    assert changes == Changes([ChildSpec("api", [NEW]), ChildSpec(f"worker-{newest.id}", [NEW, newest.id])])
    assert factory.tick() is None  # until its worker made its versions current
    assert api.started == []

    started(workers, {"acme": "c" * 40})
    assert factory.tick() == Changes(stop=[old_worker])
    assert [g.factory for g in generations.all_generations(workers.directory)] == [NEW]

    assert factory.tick() is None
    assert api.started == ["gh:o/acme#1", "gh:o/acme#2"]  # manual starts: the hold released, `$handoff` given
    assert api.scheduler is True
    assert not factory.breaking.active


def test_a_new_fact_dev_finishes_the_breaking_upgrade_the_last_one_left(
    project_settings: Settings, workers: Generations, remote: MigratingRemote
) -> None:
    api = FakeApi(scheduler=False)
    factory = _factory(project_settings, workers, remote, api)
    factory.tick()
    api.finish()

    factory.restart()  # `fact dev` was killed while the runs stopped
    factory.tick()
    started(workers, {"acme": "c" * 40})
    factory.tick()
    factory.tick()

    assert api.started == ["gh:o/acme#1", "gh:o/acme#2"]
    assert api.scheduler is False  # it was off before the upgrade
    assert current_pin(project_settings).name == NEW
    assert not factory.breaking.active


def test_a_task_restarted_before_a_crash_is_not_started_twice(
    project_settings: Settings, workers: Generations, remote: MigratingRemote
) -> None:
    api = FakeApi()
    factory = _factory(project_settings, workers, remote, api)
    state = factory.breaking.path
    factory.tick()
    api.finish()
    factory.tick()
    started(workers, {"acme": "c" * 40})
    factory.tick()
    data = json.loads(state.read_text(encoding="utf-8"))
    state.write_text(json.dumps({**data, "restarted": ["r1"]}), encoding="utf-8")

    factory.restart()
    factory.tick()

    assert api.started == ["gh:o/acme#2"]


def test_an_upgrade_with_only_additive_migrations_stops_no_run(
    project_settings: Settings, workers: Generations, remote: MigratingRemote
) -> None:
    api = FakeApi()
    remote.migration = 'op.create_table("b")'
    factory = Factory(project_settings, workers, remote, api)
    remote.main = NEW

    assert factory.tick() is not None  # the new generation starts at once

    assert current_pin(project_settings).name == NEW
    assert read_state(factory.breaking.path) is None
    assert (api.scheduler, api.stopping) == (True, [])


def test_the_upgrade_file_is_not_taken_for_a_process_record(project_settings: Settings) -> None:
    run_dir = RunDir(project_settings.data_dir / "run")
    run_dir.path.mkdir(parents=True)
    (run_dir.path / "upgrade.json").write_text("{}", encoding="utf-8")

    assert run_dir.records() == {}
    assert (run_dir.path / "upgrade.json").exists()


def test_the_api_client_reads_open_runs_and_takes_a_refused_start_as_done(monkeypatch: pytest.MonkeyPatch) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/runs":
            status = request.url.params["status"]
            return httpx.Response(200, json=[{"id": "r1", "task_key": "gh:o/acme#1"}] if status == "waiting" else [])
        return httpx.Response(409, json={"detail": "gh:o/acme#1 already has an active run"})

    transport = httpx.MockTransport(answer)
    monkeypatch.setattr(projects, "make_client", lambda _policy: httpx.Client(base_url="http://x", transport=transport))
    api = HttpUpgradeApi()

    assert api.open_runs(["acme"]) == [OpenRun("r1", "gh:o/acme#1", "acme")]
    assert api.start("gh:o/acme#1", "acme") == '{"detail":"gh:o/acme#1 already has an active run"}'
    api.stop("r1", REASON)  # already ended: nothing more to do
