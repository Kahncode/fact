from pathlib import Path

import httpx
import pytest
from fact.cli import follow, projects
from fact.cli.follow import CHECK_INTERVAL_S, Follower
from fact.core.settings import Settings

OLD, NEW = "a" * 40, "b" * 40


class FakeScheduler:
    """The API's scheduler switch and active-run count, keeping state across calls."""

    def __init__(self, *, on: bool = True, active: int = 0) -> None:
        self.on, self.active, self.up = on, active, True

    def handle(self, request: httpx.Request) -> httpx.Response:
        if not self.up:
            msg = "refused"
            raise httpx.ConnectError(msg, request=request)
        path = request.url.path.removeprefix("/api/v1/scheduler")
        if request.method == "POST":
            self.on = path == "/on"
        if path == "/active":
            return httpx.Response(200, json={"active": self.active})
        return httpx.Response(200, json={"on": self.on})


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def scheduler(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> FakeScheduler:
    fake = FakeScheduler()
    transport = httpx.MockTransport(fake.handle)
    monkeypatch.setattr(
        projects, "make_client", lambda *_: httpx.Client(base_url=settings.api_url, transport=transport)
    )
    return fake


@pytest.fixture
def remote(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """`origin/main` as the follower sees it; `pin` checks that SHA out."""
    state = {"main": OLD}
    monkeypatch.setattr(follow, "latest", lambda ref, _repo: state[ref])
    monkeypatch.setattr(follow, "pin", lambda *_: settings.pinned_dir / "factory" / state["main"])
    return state


def _follower(settings: Settings, clock: Clock) -> Follower:
    return Follower(
        settings,
        "main",
        uv="uv",
        pinned=OLD,
        log=lambda _: None,
        clock=clock,
    )


def _tick(follower: Follower, clock: Clock) -> str:
    """Run one check; return the SHA pinned after it."""
    clock.now += CHECK_INTERVAL_S
    follower()
    return follower.pinned


def test_upgrades_once_main_moved_and_no_run_is_active(
    settings: Settings, scheduler: FakeScheduler, remote: dict[str, str]
) -> None:
    clock = Clock()
    follower = _follower(settings, clock)
    assert _tick(follower, clock) == OLD  # main has not moved
    assert scheduler.on

    remote["main"] = NEW
    scheduler.active = 1
    assert _tick(follower, clock) == OLD  # pending: nothing new starts while the run goes on
    assert not scheduler.on
    assert follower.pending == NEW

    scheduler.active = 0
    assert _tick(follower, clock) == NEW  # pinned: `fact dev` starts it
    assert follower.pending is None
    scheduler.up = False  # the restarted API keeps the switch off, and is not answering yet
    follower()
    scheduler.up = True
    clock.now += 1
    follower()
    assert scheduler.on  # on again, as it was before the upgrade


def test_keeps_the_scheduler_off_when_it_was_off_before_the_upgrade(
    settings: Settings, scheduler: FakeScheduler, remote: dict[str, str]
) -> None:
    clock = Clock()
    follower = _follower(settings, clock)
    scheduler.on = False
    remote["main"] = NEW
    assert _tick(follower, clock) == NEW
    clock.now += 1
    follower()

    assert not scheduler.on


def test_a_restarted_follower_still_turns_the_scheduler_back_on(
    settings: Settings, scheduler: FakeScheduler, remote: dict[str, str]
) -> None:
    clock = Clock()
    remote["main"] = NEW
    scheduler.active = 1
    assert _tick(_follower(settings, clock), clock) == OLD  # pending; then `fact dev --follow` is restarted
    assert not scheduler.on

    restarted = _follower(settings, clock)
    scheduler.active = 0
    assert _tick(restarted, clock) == NEW  # the switch reads off now, but the follower turned it off
    clock.now += 1
    restarted()
    assert scheduler.on


@pytest.mark.parametrize("error", [follow.PinError("uv sync failed"), PermissionError("current is locked")])
def test_a_failed_check_keeps_the_upgrade_pending(
    settings: Settings,
    scheduler: FakeScheduler,
    remote: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    clock = Clock()
    follower = _follower(settings, clock)
    remote["main"] = NEW

    def broken_pin(*_: object) -> Path:
        raise error

    monkeypatch.setattr(follow, "pin", broken_pin)
    assert _tick(follower, clock) == OLD
    assert follower.pending == NEW
    assert not scheduler.on
