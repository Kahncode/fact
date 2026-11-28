"""Fakes shared by the follower and breaking-upgrade tests: pins on disk, the remote, generations up."""

from pathlib import Path

from fact.cli import follow
from fact.cli.generations import Generations
from fact.cli.processes import ChildSpec, RunDir
from fact.cli.use import factory_checkout
from fact.core import generations
from fact.core.settings import Settings

OLD, NEW = "a" * 40, "b" * 40
HOME, HOME_2 = "c" * 40, "d" * 40


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def prepare(settings: Settings, sha: str) -> Path:
    """Build `sha`'s checkout as `fact use` does, without making it current."""
    checkout = factory_checkout(settings, sha)
    checkout.mkdir(parents=True, exist_ok=True)
    (checkout / "pyproject.toml").write_text("", encoding="utf-8")
    return checkout


def pin(settings: Settings, sha: str) -> Path:
    """Pin `sha` as `fact use` does: its checkout, then `current`."""
    checkout = prepare(settings, sha)
    (settings.pinned_dir / "current").write_text(f"{sha}\n", encoding="utf-8")
    return checkout


class Remote:
    """`origin/main` and the project's home branch as the follower sees them; `prepare` builds `main`'s pin."""

    def __init__(self, settings: Settings) -> None:
        self.main, self.home, self.pins, self.broken = OLD, HOME, 0, False
        self._settings = settings

    def latest(self, _ref: str, _repo: Path) -> str:
        return self.main

    def prepare(self, *_: object) -> Path:
        if self.broken:
            msg = "uv sync failed"
            raise follow.PinError(msg)
        self.pins += 1
        return prepare(self._settings, self.main)


def started(workers: Generations, projects: dict[str, str]) -> generations.Generation:
    """Record the newest generation's home SHAs and its start, as its worker does."""
    newest = generations.newest(workers.directory)
    assert newest is not None
    generations.write(workers.directory, generations.Generation(newest.id, newest.factory, projects))
    generations.mark_started(workers.directory, newest.id)
    return newest


def make_workers(settings: Settings, run_dir: Path) -> Generations:
    """Return the generations manager, its first generation of `OLD` up with the project at `HOME`."""
    manager = Generations(
        settings,
        worker_spec=lambda checkout, g: ChildSpec(f"worker-{g.id}", [checkout.name, g.id]),
        api_spec=lambda checkout: ChildSpec("api", [checkout.name]),
        in_use=lambda _: set(),
        ready=lambda checkout: checkout.is_dir(),
        log=lambda _: None,
    )
    manager.initial(pin(settings, OLD), RunDir(run_dir))
    started(manager, {"acme": HOME})
    return manager
