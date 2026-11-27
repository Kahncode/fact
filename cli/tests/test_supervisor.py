# ruff: noqa: PLR2004
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fact.cli import supervisor
from fact.cli.supervisor import Child, ChildSpec, StopRequestedError, run_all

SLEEPER = [sys.executable, "-c", "import time; print('up', flush=True); time.sleep(60)"]
# Ignores the interrupt (SIGINT, or Ctrl-Break on Windows), so only terminate/kill end it.
STUBBORN = [
    sys.executable,
    "-c",
    (
        "import signal, time; "
        "signal.signal(getattr(signal, 'SIGBREAK', signal.SIGINT), signal.SIG_IGN); "
        "print('ready', flush=True); time.sleep(60)"
    ),
]


def _popen(argv: list[str]) -> subprocess.Popen[str]:
    kwargs: dict = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if supervisor.IS_WINDOWS else {"start_new_session": True}
    )
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True, **kwargs)  # noqa: S603
    assert proc.stdout is not None
    proc.stdout.readline()  # wait until the child has installed its handlers
    return proc


def test_prefixes_output_and_stops_all_when_one_exits(capsys: pytest.CaptureFixture[str]) -> None:
    quitter = [sys.executable, "-c", "import time; time.sleep(1); print('bye'); raise SystemExit(3)"]
    started = time.monotonic()

    code = run_all([ChildSpec("sleeper", SLEEPER), ChildSpec("q", quitter)])

    assert code == 3
    assert time.monotonic() - started < 30  # the sleeper was stopped, not waited for
    out = capsys.readouterr().out.splitlines()
    assert "sleeper | up" in out
    assert "q       | bye" in out
    assert "fact    | q exited with code 3; stopping the others" in out


def test_ctrl_c_stops_children_and_returns_130(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[Child] = []
    real_first_exit = supervisor.first_exit

    def interrupt_once_running(children: list[Child]) -> tuple[str, int] | None:
        if len(children) == 2:
            started.extend(children)
            raise KeyboardInterrupt
        return real_first_exit(children)

    monkeypatch.setattr(supervisor, "first_exit", interrupt_once_running)

    assert run_all([ChildSpec("a", SLEEPER), ChildSpec("b", SLEEPER)]) == 130
    assert [c.proc.poll() is not None for c in started] == [True, True]


def test_stop_escalates_for_a_child_ignoring_interrupt() -> None:
    proc = _popen(STUBBORN)
    started = time.monotonic()

    supervisor.stop_all([Child("stubborn", proc)], timeout=0.5)

    assert proc.poll() is not None
    assert time.monotonic() - started < 10


def test_port_wait_aborts_when_a_process_exits(capsys: pytest.CaptureFixture[str]) -> None:
    early_exit = [sys.executable, "-c", "pass"]
    code = run_all([ChildSpec("a", early_exit), ChildSpec("b", SLEEPER, wait_for=("127.0.0.1", 1))])
    assert code == 1
    assert "error: a exited while waiting for 127.0.0.1:1" in capsys.readouterr().out


def test_signal_death_maps_to_shell_exit_code() -> None:
    class Dead:
        def poll(self) -> int:
            return -9

    assert supervisor.first_exit([Child("x", Dead())]) == ("x", 137)  # type: ignore[arg-type]


def test_child_env_is_added(capsys: pytest.CaptureFixture[str]) -> None:
    show = [sys.executable, "-c", "import os; print(os.environ['FACT_TEST_VAR'])"]

    assert run_all([ChildSpec("e", show, env={"FACT_TEST_VAR": "from-spec"})]) == 0
    assert "e    | from-spec" in capsys.readouterr().out.splitlines()


def test_restarter_replaces_named_children_and_keeps_the_others(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    starts = tmp_path / "a-starts"  # `a` adds a line each time it starts
    keeper = [sys.executable, "-c", f"open({str(starts)!r}, 'a').write('up\\n'); import time; time.sleep(60)"]
    replacement = [sys.executable, "-c", "print('new', flush=True); raise SystemExit(4)"]
    restarted = []

    def restarter() -> list[ChildSpec] | None:
        if restarted or not starts.exists():  # restart once, after `a` is up
            return None
        restarted.append(True)
        return [ChildSpec("b", replacement)]

    code = run_all([ChildSpec("a", keeper), ChildSpec("b", SLEEPER)], restarter)

    assert code == 4  # the replacement ran, and its exit ended the supervisor
    assert starts.read_text(encoding="utf-8").splitlines() == ["up"]  # `a` was not restarted
    assert "b    | new" in capsys.readouterr().out.splitlines()


def test_a_stop_request_stops_every_child_and_returns_0(capsys: pytest.CaptureFixture[str]) -> None:
    def restarter() -> None:
        msg = "shutdown: no run active, stopping"
        raise StopRequestedError(msg)

    assert run_all([ChildSpec("a", SLEEPER), ChildSpec("b", SLEEPER)], restarter) == 0
    assert "fact | shutdown: no run active, stopping" in capsys.readouterr().out.splitlines()
