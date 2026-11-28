"""`fact cleanup`: drained generations lose their pins, Temporal versions and records; nothing else does."""

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
from fact.cli import removal
from fact.cli.cleanup import run_cleanup
from fact.cli.dev_lock import LOCK_NAME, DevLock, record_pin
from fact.cli.drainage import Version
from fact.cli.removal import Report
from fact.core import generations
from fact.core.generations import Generation
from fact.core.settings import Settings

HOMES = {n: str(n) * 40 for n in range(1, 5)}  # generation n's project home SHA


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)  # noqa: S603, S607
    return result.stdout.strip()


class Temporal:
    """Reports `in_use` (None: down) and records the versions deleted; refuses those in `refuse`."""

    def __init__(self) -> None:
        self.in_use: set[Version] | None = set()
        self.refuse: set[Version] = set()
        self.deleted: list[Version] = []

    def check(self, versions: Sequence[Version]) -> set[Version] | None:
        return None if self.in_use is None else self.in_use & set(versions)

    def delete(self, versions: Sequence[Version]) -> dict[Version, str]:
        self.deleted += [v for v in versions if v not in self.refuse]
        return dict.fromkeys(self.refuse & set(versions), "it has active pollers")


@pytest.fixture
def temporal(monkeypatch: pytest.MonkeyPatch) -> Temporal:
    fake = Temporal()
    monkeypatch.setattr("fact.cli.drainage.versions_in_use", lambda _s, versions: fake.check(versions))
    monkeypatch.setattr("fact.cli.temporal_admin.delete_versions", lambda _s, versions: fake.delete(versions))
    return fake


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """Make a factory source tree with four commits."""
    repo = tmp_path / "source"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    for n in range(4):
        _git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "--allow-empty", "-m", str(n), cwd=repo)
    return repo


def _factory(settings: Settings, source: Path) -> list[Generation]:
    """Pin each commit as a worktree, with generation n+1 of commit n (each with its project pin); the last current."""
    shas = _git("rev-list", "--reverse", "HEAD", cwd=source).split()
    directory = generations.generations_dir(settings.data_dir)
    found = []
    for n, sha in enumerate(shas, start=1):
        _git("worktree", "add", "--detach", str(settings.pinned_dir / "factory" / sha), sha, cwd=source)
        (settings.pinned_dir / "projects" / "fact" / HOMES[n] / ".git").mkdir(parents=True)
        generation = Generation(f"{n}-{sha[:12]}", sha, {"fact": HOMES[n]})
        generations.write(directory, generation)
        found.append(generation)
    (settings.pinned_dir / "current").write_text(f"{shas[-1]}\n", encoding="utf-8")
    return found


def _pins(settings: Settings) -> set[str]:
    found = [*settings.pinned_dir.glob("factory/*"), *settings.pinned_dir.glob("projects/*/*")]
    return {p.relative_to(settings.pinned_dir).as_posix() for p in found}


def _clean(settings: Settings, source: Path, *, dry_run: bool = False, older_than: str | None = None) -> Report:
    report = Report(dry_run=dry_run, echo=lambda _line: None)
    run_cleanup(settings, report, older_than, repo=source)
    return report


def test_cleanup_removes_only_the_drained_generations(settings: Settings, source: Path, temporal: Temporal) -> None:
    drained_1, drained_2, draining, current = _factory(settings, source)
    temporal.in_use = set(draining.versions())

    report = _clean(settings, source)

    assert report.failures == 0
    assert temporal.deleted == drained_1.versions() + drained_2.versions()
    kept = generations.all_generations(generations.generations_dir(settings.data_dir))
    assert [g.id for g in kept] == [draining.id, current.id]
    assert _pins(settings) == {
        "factory/" + draining.factory,
        "factory/" + current.factory,
        "projects/fact/" + HOMES[3],
        "projects/fact/" + HOMES[4],
    }
    listed = _git("worktree", "list", "--porcelain", cwd=source)
    assert drained_1.factory not in listed
    assert drained_2.factory not in listed
    assert draining.factory in listed


@pytest.mark.parametrize(
    ("in_use", "refused"),
    [(None, False), (set(), True)],
    ids=["temporal-down-keeps-every-older-generation", "a-refused-version-keeps-its-generation"],
)
def test_cleanup_keeps_what_temporal_does_not_let_go(
    settings: Settings, source: Path, temporal: Temporal, in_use: set[Version] | None, *, refused: bool
) -> None:
    first, *_ = _factory(settings, source)
    temporal.in_use = in_use
    if refused:
        temporal.refuse = set(first.versions())

    _clean(settings, source)

    assert (settings.pinned_dir / "factory" / first.factory).is_dir()
    assert (settings.pinned_dir / "projects" / "fact" / HOMES[1]).is_dir()
    assert generations.read(generations.generations_dir(settings.data_dir), first.id) == first


@pytest.mark.parametrize("running", [True, False], ids=["while-fact-dev-runs-from-it", "once-fact-dev-stopped"])
def test_the_pin_fact_dev_runs_from_is_kept_while_it_runs(
    settings: Settings, source: Path, temporal: Temporal, *, running: bool
) -> None:
    first, *_ = _factory(settings, source)
    temporal.in_use = set()
    lock = DevLock(settings.data_dir / LOCK_NAME)
    lock.acquire()  # held here as by `fact dev --in-pin`; `held()` asks through another handle
    record_pin(settings.data_dir, first.factory)
    if not running:
        lock.release()
    try:
        _clean(settings, source)
    finally:
        lock.release()

    assert (settings.pinned_dir / "factory" / first.factory).is_dir() == running
    assert not (settings.pinned_dir / "projects" / "fact" / HOMES[1]).exists()  # its generation is drained still


def test_a_pin_that_cannot_be_deleted_stays_trash_until_the_next_cleanup(
    settings: Settings, source: Path, temporal: Temporal, monkeypatch: pytest.MonkeyPatch
) -> None:
    _factory(settings, source)
    temporal.in_use = set()
    held = settings.pinned_dir / "projects" / "fact" / f".trash-{HOMES[1]}"
    real_delete = removal.delete
    monkeypatch.setattr(removal, "delete", lambda p: "held by another process" if p == held else real_delete(p))

    assert _clean(settings, source).failures == 1

    assert held.is_dir()
    assert not (settings.pinned_dir / "projects" / "fact" / HOMES[1]).exists()  # never taken for the pin again
    monkeypatch.setattr(removal, "delete", real_delete)
    assert _clean(settings, source).failures == 0
    assert not held.exists()


def _snapshot(root: Path) -> dict[str, float]:
    return {p.relative_to(root).as_posix(): p.stat().st_mtime for p in root.rglob("*")}


def test_dry_run_changes_nothing(settings: Settings, source: Path, temporal: Temporal) -> None:
    _factory(settings, source)
    (settings.pinned_dir / "factory" / ".trash-old").mkdir()
    stale = '{"role": "api", "build": "x", "pid": 1, "started": 0, "port": null, "command": []}'
    (settings.data_dir / "run" / "api.json").write_text(stale, encoding="utf-8")
    before = _snapshot(settings.data_dir)

    report = _clean(settings, source, dry_run=True, older_than="30d")

    assert report.removed  # it said what it would remove
    assert temporal.deleted == []
    assert _snapshot(settings.data_dir) == before
