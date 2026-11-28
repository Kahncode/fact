"""`fact dev --follow` adds a worker generation when the ref or a project's home moves, never waiting for runs."""

from pathlib import Path

import pytest
from fact.cli import follow
from fact.cli.follow import CHECK_INTERVAL_S, Follower
from fact.cli.generations import Generations
from fact.cli.processes import ChildSpec, RunDir
from fact.cli.supervisor import Changes
from fact.cli.use import factory_checkout
from fact.core import generations
from fact.core.settings import ProjectRef, Settings

OLD, NEW = "a" * 40, "b" * 40
HOME, HOME_2 = "c" * 40, "d" * 40


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _pin(settings: Settings, sha: str) -> Path:
    """Pin `sha` as `fact use` does: its checkout, then `current`."""
    checkout = factory_checkout(settings, sha)
    checkout.mkdir(parents=True, exist_ok=True)
    (checkout / "pyproject.toml").write_text("", encoding="utf-8")
    (settings.pinned_dir / "current").write_text(f"{sha}\n", encoding="utf-8")
    return checkout


class Remote:
    """`origin/main` and the project's home branch as the follower sees them; `pin` pins `main`."""

    def __init__(self, settings: Settings) -> None:
        self.main, self.home, self.pins, self.broken = OLD, HOME, 0, False
        self._settings = settings

    def latest(self, _ref: str, _repo: Path) -> str:
        return self.main

    def pin(self, *_: object) -> Path:
        if self.broken:
            msg = "uv sync failed"
            raise follow.PinError(msg)
        self.pins += 1
        return _pin(self._settings, self.main)


@pytest.fixture
def project_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"projects": [ProjectRef(name="acme", home="o/acme")]})


@pytest.fixture
def remote(project_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Remote:
    fake = Remote(project_settings)
    monkeypatch.setattr(follow, "latest", fake.latest)
    monkeypatch.setattr(follow, "pin", fake.pin)
    return fake


@pytest.fixture
def workers(project_settings: Settings, tmp_path: Path) -> Generations:
    """Return the generations manager, its first generation of `OLD` up with the project at `HOME`."""
    manager = Generations(
        project_settings,
        worker_spec=lambda checkout, g: ChildSpec(f"worker-{g.id}", [checkout.name, g.id]),
        api_spec=lambda checkout: ChildSpec("api", [checkout.name]),
        in_use=lambda _: set(),
        ready=lambda checkout: checkout.is_dir(),
        log=lambda _: None,
    )
    manager.initial(_pin(project_settings, OLD), RunDir(tmp_path / "run"))
    _started(manager, {"acme": HOME})
    return manager


def _started(workers: Generations, projects: dict[str, str]) -> generations.Generation:
    """Record the newest generation's home SHAs and its start, as its worker does."""
    newest = generations.newest(workers.directory)
    assert newest is not None
    generations.write(workers.directory, generations.Generation(newest.id, newest.factory, projects))
    generations.mark_started(workers.directory, newest.id)
    return newest


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
