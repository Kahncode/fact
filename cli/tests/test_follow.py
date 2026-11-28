"""`fact dev --follow` adds a worker generation when the ref or a project's home moves, never waiting for runs."""

from pathlib import Path

import pytest
from fact.cli import follow
from fact.cli.breaking import BreakingUpgrade
from fact.cli.follow import CHECK_INTERVAL_S, Follower
from fact.cli.generations import Generations
from fact.cli.processes import ChildSpec
from fact.cli.supervisor import Changes
from fact.core import generations
from fact.core.settings import ProjectRef, Settings
from follow_fakes import HOME_2, NEW, OLD, Clock, Remote, make_workers
from follow_fakes import started as _started


class NoApi:
    """An upgrade with no breaking migration never touches the scheduler nor a run."""

    def __getattr__(self, name: str) -> object:  # noqa: D105
        pytest.fail(f"the API was called: {name}")


@pytest.fixture
def project_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"projects": [ProjectRef(name="acme", home="o/acme")]})


@pytest.fixture
def remote(project_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Remote:
    fake = Remote(project_settings)
    monkeypatch.setattr(follow, "latest", fake.latest)
    monkeypatch.setattr(follow, "prepare", fake.prepare)
    return fake


@pytest.fixture
def workers(project_settings: Settings, tmp_path: Path) -> Generations:
    return make_workers(project_settings, tmp_path / "run")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def follower(project_settings: Settings, workers: Generations, remote: Remote, clock: Clock) -> Follower:
    return Follower(
        project_settings,
        "main",
        uv="uv",
        workers=workers,
        breaking=BreakingUpgrade(project_settings, workers=workers, log=lambda _: None, api=NoApi()),
        log=lambda _: None,
        clock=clock,
        resolve_home=lambda _home: remote.home,
    )


def _tick(follower: Follower, clock: Clock) -> Changes | None:
    clock.now += CHECK_INTERVAL_S
    return follower()


def test_main_moving_pins_it_and_starts_its_generation_next_to_the_old_one(
    follower: Follower, workers: Generations, remote: Remote, clock: Clock
) -> None:
    assert _tick(follower, clock) is None  # nothing moved
    remote.main = NEW  # no scheduler is touched and no run is waited for: the API is never asked

    changes = _tick(follower, clock)

    newest = generations.newest(workers.directory)
    assert newest is not None
    assert newest.factory == NEW
    assert changes == Changes([ChildSpec("api", [NEW]), ChildSpec(f"worker-{newest.id}", [NEW, newest.id])])
    old = generations.all_generations(workers.directory)[0]
    assert old.factory == OLD  # kept: it serves its runs until they end


def test_a_home_moving_starts_a_generation_of_the_same_pin_and_keeps_the_api(
    follower: Follower, workers: Generations, remote: Remote, clock: Clock
) -> None:
    remote.home = HOME_2  # a commit to the project's `.fact/workflows/`

    changes = _tick(follower, clock)

    newest = generations.newest(workers.directory)
    assert newest is not None
    assert remote.pins == 0
    assert changes == Changes([ChildSpec(f"worker-{newest.id}", [OLD, newest.id])])


def test_nothing_is_added_while_the_newest_generation_is_starting(
    follower: Follower, workers: Generations, remote: Remote, clock: Clock
) -> None:
    remote.home = HOME_2
    assert _tick(follower, clock) is not None
    remote.main = NEW

    assert _tick(follower, clock) is None  # its worker has not made its versions current yet

    _started(workers, {"acme": HOME_2})
    assert _tick(follower, clock) is not None


def test_a_failed_check_is_retried_at_the_next(
    follower: Follower, workers: Generations, remote: Remote, clock: Clock
) -> None:
    remote.main, remote.broken = NEW, True
    assert _tick(follower, clock) is None

    remote.broken = False
    assert _tick(follower, clock) is not None
    newest = generations.newest(workers.directory)
    assert newest is not None
    assert newest.factory == NEW
