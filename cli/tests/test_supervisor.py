# ruff: noqa: PLR2004
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import psutil
import pytest
from fact.cli import processes, supervisor
from fact.cli.processes import Child, ChildSpec, ProcessRecord, RunDir
from fact.cli.supervisor import StopRequestedError, Supervisor, run_all

SLEEPER = [sys.executable, "-c", "import time; print('up', flush=True); time.sleep(60)"]
# Exits 0 once its stop file appears, as the API and the worker do.
GRACEFUL = [
    sys.executable,
    "-c",
    (
        "import os, pathlib, time\n"
        "stop = pathlib.Path(os.environ['FACT_STOP_FILE'])\n"
        "print('ready', flush=True)\n"
        "while not stop.exists(): time.sleep(0.05)\n"
        "print('stopped gracefully', flush=True)\n"
    ),
]


def _sleeper(name: str) -> ChildSpec:
    return ChildSpec(name, SLEEPER, graceful=False)  # killed, so the tests never wait for a graceful stop


@pytest.fixture
def run_dir(tmp_path: Path) -> RunDir:
    return RunDir(tmp_path / "run")


@pytest.fixture
def started() -> Iterator[list[psutil.Process]]:
    """Collect the processes a test leaves running on purpose, to kill them at the end."""
    procs: list[psutil.Process] = []
    yield procs
    for proc in procs:
        processes.kill_tree(proc)


def _wait_exit(child: Child) -> None:
    assert child.popen is not None
    child.popen.wait(timeout=15)


def test_a_restarted_supervisor_adopts_the_live_children(run_dir: RunDir) -> None:
    specs = [_sleeper("a"), _sleeper("b")]
    first = Supervisor(specs, run_dir)
    first.start_missing()
    pids = {name: child.proc.pid for name, child in first.children.items()}
    assert {n: r.pid for n, r in run_dir.records().items()} == pids
    # `fact dev` is killed: nothing stops its children; the next one adopts them

    second = Supervisor(specs, run_dir)
    second.adopt()
    second.start_missing()

    assert {name: child.proc.pid for name, child in second.children.items()} == pids  # no duplicate started
    second.stop_all()
    assert run_dir.records() == {}
    assert not any(psutil.pid_exists(pid) and psutil.Process(pid).is_running() for pid in pids.values())


def test_stale_records_are_removed_and_their_process_started_again(run_dir: RunDir) -> None:
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    me = psutil.Process()
    run_dir.write("a", ProcessRecord("a", "source", dead.pid, time.time(), None, SLEEPER))
    run_dir.write("b", ProcessRecord("b", "source", me.pid, me.create_time() - 100, None, SLEEPER))  # a reused pid
    sup = Supervisor([_sleeper("a"), _sleeper("b")], run_dir)

    sup.adopt()
    assert run_dir.records() == {}
    sup.start_missing()

    try:
        assert {c.proc.pid for c in sup.children.values()}.isdisjoint({dead.pid, me.pid})
        assert set(run_dir.records()) == {"a", "b"}
    finally:
        sup.stop_all()
    assert me.is_running()  # the process that reused the pid was left alone


def test_an_adopted_child_running_another_command_is_replaced(run_dir: RunDir) -> None:
    old = Supervisor([_sleeper("api")], run_dir)
    old.start_missing()
    old_proc = old.children["api"].proc
    new_spec = replace(_sleeper("api"), argv=[*SLEEPER, "new"], build="abc123")
    new = Supervisor([new_spec], run_dir)

    new.adopt()
    new.start_missing()

    try:
        assert not old_proc.is_running()
        assert run_dir.records()["api"].build == "abc123"
    finally:
        new.stop_all()


def test_a_child_that_exits_is_started_again(run_dir: RunDir, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(supervisor, "RESTART_DELAY_S", 0.0)
    starts = tmp_path / "starts"
    quitter = [sys.executable, "-c", f"open({str(starts)!r}, 'a').write('up\\n'); raise SystemExit(3)"]
    sup = Supervisor([ChildSpec("q", quitter, graceful=False)], run_dir)

    sup.start_missing()
    _wait_exit(sup.children["q"])
    sup.reap()
    assert "q" not in run_dir.records()  # removed once it exited
    sup.start_missing()
    _wait_exit(sup.children["q"])
    sup.reap()

    assert starts.read_text(encoding="utf-8").splitlines() == ["up", "up"]


def test_a_graceful_child_stops_on_its_stop_file(run_dir: RunDir, capsys: pytest.CaptureFixture[str]) -> None:
    sup = Supervisor([ChildSpec("w", GRACEFUL)], run_dir)
    sup.start_missing()
    child = sup.children["w"]

    sup.stop_all()

    assert child.exit_code() == 0  # it exited by itself, not killed
    assert not run_dir.stop_path("w").exists()
    assert "w    | stopped gracefully" in capsys.readouterr().out.splitlines()


def test_killing_the_tree_stops_a_child_ignoring_its_stop_file(run_dir: RunDir) -> None:
    spec = ChildSpec("s", SLEEPER)  # graceful, but never looks at its stop file
    child = processes.start(spec, run_dir)

    processes.stop(child, run_dir, timeout=0.5)

    assert child.exited()
    assert run_dir.records() == {}


def test_run_all_prefixes_output_and_stops_every_child_on_a_stop_request(
    run_dir: RunDir, capsys: pytest.CaptureFixture[str]
) -> None:
    def restarter() -> None:
        if "up" in run_dir.log_path("a").read_text(encoding="utf-8"):
            msg = "shutdown: no run active, stopping"
            raise StopRequestedError(msg)

    assert run_all([_sleeper("a"), _sleeper("b")], run_dir, restarter) == 0
    assert run_dir.records() == {}
    out = capsys.readouterr().out.splitlines()
    assert "a    | up" in out
    assert "fact | shutdown: no run active, stopping" in out


def test_ctrl_c_stops_every_child_and_returns_130(run_dir: RunDir) -> None:
    def restarter() -> None:
        if len(run_dir.records()) == 2:
            raise KeyboardInterrupt

    assert run_all([_sleeper("a"), _sleeper("b")], run_dir, restarter) == 130
    assert run_dir.records() == {}


def test_restarter_replaces_named_children_and_keeps_the_others(run_dir: RunDir) -> None:
    replaced: list[int] = []

    def restarter() -> list[ChildSpec] | None:
        records = run_dir.records()
        if not replaced and len(records) == 2:
            replaced.append(records["a"].pid)
            return [replace(_sleeper("b"), build="new")]
        if replaced and records.get("b", ProcessRecord("b", "", 0, 0, None, [])).build == "new":
            assert records["a"].pid == replaced[0]  # `a` kept running
            raise KeyboardInterrupt
        return None

    assert run_all([_sleeper("a"), _sleeper("b")], run_dir, restarter) == 130


def test_a_record_holds_role_build_pid_start_time_port_and_command(
    run_dir: RunDir, started: list[psutil.Process]
) -> None:
    child = processes.start(ChildSpec("api", SLEEPER, build="abc", port=8100), run_dir)
    started.append(child.proc)

    record = run_dir.records()["api"]

    assert (record.role, record.build, record.pid, record.port, record.command) == (
        "api",
        "abc",
        child.proc.pid,
        8100,
        SLEEPER,
    )
    assert processes.find_process(record) is not None
