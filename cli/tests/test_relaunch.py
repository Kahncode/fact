"""`fact dev` runs its own CLI from the newest pin, restarting itself when the pin's CLI changed."""

import subprocess
import sys
from pathlib import Path

import pytest
from fact.cli import relaunch
from fact.cli.relaunch import MARKER, RESTART, SelfRestart, launch
from fact.cli.supervisor import DetachError
from fact.cli.use import factory_checkout
from fact.core.settings import Settings

A, B = "a" * 40, "b" * 40


def _pin(settings: Settings, sha: str, *, in_pin: bool = True) -> Path:
    checkout = factory_checkout(settings, sha)
    (checkout / MARKER).parent.mkdir(parents=True, exist_ok=True)
    (checkout / "pyproject.toml").write_text("", encoding="utf-8")
    if in_pin:
        (checkout / MARKER).write_text("", encoding="utf-8")
    (settings.pinned_dir / "current").write_text(f"{sha}\n", encoding="utf-8")
    return checkout


def _self_restart(settings: Settings, **kwargs: object) -> SelfRestart:
    clock = iter(range(0, 10_000, 10))
    return SelfRestart(settings, repo=Path(), log=lambda _: None, clock=lambda: next(clock), **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(("cli_changed", "restarts"), [(True, True), (False, False)])
def test_a_new_pin_restarts_fact_dev_only_when_its_cli_changed(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, *, cli_changed: bool, restarts: bool
) -> None:
    _pin(settings, A)
    compared: list[tuple[str, str]] = []
    monkeypatch.setattr(relaunch, "changed", lambda _repo, old, new, _paths: compared.append((old, new)) or cli_changed)
    restart = _self_restart(settings, running=A)
    assert restart() is None

    _pin(settings, B)
    if restarts:
        with pytest.raises(DetachError) as exc:
            restart()
        assert exc.value.code == RESTART
    else:
        assert restart() is None
        assert restart() is None  # compared once per pin
    assert compared == [(A, B)]


def test_a_launched_fact_dev_exits_alone_once_its_launcher_is_gone(settings: Settings) -> None:
    _pin(settings, A)
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()

    with pytest.raises(DetachError) as exc:
        _self_restart(settings, running=A, launcher=gone.pid)()
    assert exc.value.code == 0


def test_the_launcher_runs_fact_dev_from_each_new_pin_until_it_ends(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin(settings, A)
    ran: list[str] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        ran.append(argv[0])
        if len(ran) == 1:
            _pin(settings, B)  # the follower pinned B, whose CLI changed
            return subprocess.CompletedProcess(argv, RESTART)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(relaunch.subprocess, "run", fake_run)

    code = launch(settings, lambda checkout: [checkout.name], {}, supervise=lambda: pytest.fail("not supervised here"))

    assert code == 0
    assert ran == [A, B]


def test_the_launcher_supervises_itself_for_a_pin_that_cannot_run_in_pin(settings: Settings) -> None:
    _pin(settings, A, in_pin=False)

    assert launch(settings, lambda _: pytest.fail("not launched"), {}, supervise=lambda: 130) == 130  # noqa: PLR2004
