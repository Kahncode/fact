"""`fact dev --pinned` runs one worker per generation that should run, and stops one whose versions are drained."""

from pathlib import Path

import psutil
import pytest
from fact.cli.drainage import Version
from fact.cli.generations import CHECK_INTERVAL_S, Generations
from fact.cli.processes import ChildSpec, ProcessRecord, RunDir
from fact.cli.supervisor import Changes
from fact.cli.use import factory_checkout
from fact.core import generations
from fact.core.generations import Generation
from fact.core.settings import Settings

A, B = "a" * 40, "b" * 40
HOME = "c" * 40


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Temporal:
    """The versions Temporal says need a worker; None while it cannot be asked."""

    def __init__(self) -> None:
        self.in_use: set[Version] | None = set()

    def __call__(self, versions: list[Version]) -> set[Version] | None:
        return None if self.in_use is None else self.in_use & set(versions)


def _pin(settings: Settings, sha: str) -> Path:
    checkout = factory_checkout(settings, sha)
    checkout.mkdir(parents=True, exist_ok=True)
    (settings.pinned_dir / "current").write_text(f"{sha}\n", encoding="utf-8")
    (checkout / "pyproject.toml").write_text("", encoding="utf-8")
    return checkout


@pytest.fixture
def temporal() -> Temporal:
    return Temporal()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def manager(settings: Settings, temporal: Temporal, clock: Clock) -> Generations:
    return Generations(
        settings,
        worker_spec=lambda checkout, g: ChildSpec(f"worker-{g.id}", [checkout.name, g.id]),
        api_spec=lambda checkout: ChildSpec("api", [checkout.name]),
        in_use=temporal,
        ready=lambda checkout: checkout.is_dir(),
        log=lambda _: None,
        clock=clock,
    )


def _tick(manager: Generations, clock: Clock) -> Changes | None:
    clock.now += CHECK_INTERVAL_S
    return manager()


def _record_home(settings: Settings, generation: Generation, home: str = HOME) -> Generation:
    """Record a home SHA, as the generation's worker does at its first start."""
    recorded = Generation(generation.id, generation.factory, {"acme": home})
    generations.write(generations.generations_dir(settings.data_dir), recorded)
    return recorded


def test_fact_use_starts_a_new_generation_next_to_the_old_one_which_stops_once_drained(
    settings: Settings, manager: Generations, temporal: Temporal, clock: Clock, tmp_path: Path
) -> None:
    [worker_a] = manager.initial(_pin(settings, A), RunDir(tmp_path / "run"))
    gen_a = _record_home(settings, generations.newest(manager.directory))
    assert worker_a.argv == [A, gen_a.id]
    assert _tick(manager, clock) is None

    _pin(settings, B)  # `fact use main` while `fact dev` runs
    temporal.in_use = set(gen_a.versions())  # A is still current until B's worker starts
    changes = _tick(manager, clock)

    assert changes is not None
    gen_b = generations.newest(manager.directory)
    assert gen_b.factory == B
    assert [s.argv for s in changes.start] == [[B], [B, gen_b.id]]  # the API from B's pin, then B's worker
    assert changes.stop == ()

    assert _tick(manager, clock) is None  # A's run waits for review: DRAINING
    temporal.in_use = set()  # its PR merged and the run ended: DRAINED
    assert _tick(manager, clock) == Changes(stop=(f"worker-{gen_a.id}",))
    assert _tick(manager, clock) is None


def test_after_a_reboot_every_generation_with_runs_starts_from_its_own_pin(
    settings: Settings, manager: Generations, temporal: Temporal, clock: Clock, tmp_path: Path
) -> None:
    directory = generations.generations_dir(settings.data_dir)
    _pin(settings, A)
    gen_old = _record_home(settings, generations.create(directory, A), "e" * 40)  # drained: never started again
    gen_a = _record_home(settings, generations.create(directory, A))
    gen_b = _record_home(settings, generations.create(directory, B))
    temporal.in_use = None  # Temporal is starting

    workers = manager.initial(_pin(settings, B), RunDir(tmp_path / "run"))
    assert [w.name for w in workers] == [f"worker-{gen_b.id}"]  # the newest, at once
    assert _tick(manager, clock) is None  # nothing is known yet: nothing starts or stops

    temporal.in_use = {*gen_a.versions(), *gen_b.versions()}
    changes = _tick(manager, clock)

    assert changes is not None
    assert [s.argv for s in changes.start] == [[A, gen_a.id]]
    assert gen_old.id not in manager.running
    assert generations.newest(directory) == gen_b  # no new generation for a pin already served


def test_a_generation_whose_pin_is_gone_is_not_started(
    settings: Settings, manager: Generations, temporal: Temporal, clock: Clock, tmp_path: Path
) -> None:
    directory = generations.generations_dir(settings.data_dir)
    gone = _record_home(settings, generations.create(directory, "d" * 40))
    manager.initial(_pin(settings, B), RunDir(tmp_path / "run"))
    temporal.in_use = set(gone.versions())

    assert _tick(manager, clock) is None


def test_a_restarted_fact_dev_adopts_an_older_generations_live_worker(
    settings: Settings, manager: Generations, tmp_path: Path
) -> None:
    directory = generations.generations_dir(settings.data_dir)
    _pin(settings, A)
    gen_a = _record_home(settings, generations.create(directory, A))
    run_dir = RunDir(tmp_path / "run")
    me = psutil.Process()  # stands in for A's worker, still running from the last `fact dev`
    run_dir.write(f"worker-{gen_a.id}", ProcessRecord("worker", A, me.pid, me.create_time(), None, [A, gen_a.id]))

    workers = manager.initial(_pin(settings, B), run_dir)

    assert [w.argv for w in workers] == [[B, generations.newest(directory).id], [A, gen_a.id]]
