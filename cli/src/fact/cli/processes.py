"""
Processes `fact dev` starts detached, each tracked by a record in `<data_dir>/run/`.

A child runs in its own session (POSIX) or process group with a hidden console of its own (Windows), its output in
`<name>.log`, so it outlives `fact dev`. `<name>.json` records its role, build, pid, process start time, port and
command; a restarted `fact dev` takes a live pid with the same start time for it (a reused pid never matches) and
removes any other record. A graceful child stops once `<name>.stop` appears (`fact.core.stop_file`); any other, or
one that outlives `STOP_TIMEOUT_S`, is stopped by killing its process tree.
"""

import contextlib
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import psutil
from fact.core.stop_file import STOP_FILE_VAR

IS_WINDOWS = sys.platform == "win32"
STOP_TIMEOUT_S = 30.0  # a graceful stop's wait before the tree is killed
KILL_TIMEOUT_S = 10.0
START_TIME_TOLERANCE_S = 1.0  # start times are read from the OS clock; a reused pid starts later than this
POLL_S = 0.2
SOURCE_BUILD = "source"  # the build of processes run from the source tree; a pinned one is its SHA
UPGRADE_FILE = "upgrade.json"  # a breaking upgrade in progress (`fact.cli.breaking`), not a process record


@dataclass(frozen=True)
class ChildSpec:
    name: str
    argv: list[str]
    build: str = SOURCE_BUILD
    port: int | None = None  # the port it serves, recorded for people and `fact cleanup`
    graceful: bool = True  # watches its stop file; otherwise stopped by killing its process tree
    wait_for: tuple[str, int] | None = None  # (host, port) that must accept connections before starting
    env: dict[str, str] = field(default_factory=dict)  # added to this process's environment
    unset: tuple[str, ...] = ()  # removed from this process's environment before `env` is added


@dataclass(frozen=True)
class ProcessRecord:
    role: str
    build: str
    pid: int
    started: float  # process start time, seconds since the epoch
    port: int | None
    command: list[str]


class RunDir:
    """`<data_dir>/run/`: a record, a stop file and a log per process."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def record_path(self, name: str) -> Path:
        return self.path / f"{name}.json"

    def stop_path(self, name: str) -> Path:
        return self.path / f"{name}.stop"

    def log_path(self, name: str) -> Path:
        return self.path / f"{name}.log"

    def records(self) -> dict[str, ProcessRecord]:
        """Every readable record by name; an unreadable one is removed."""
        found: dict[str, ProcessRecord] = {}
        for path in sorted(self.path.glob("*.json")):
            if path.name == UPGRADE_FILE:
                continue
            try:
                found[path.stem] = ProcessRecord(**json.loads(path.read_text(encoding="utf-8")))
            except OSError, ValueError, TypeError:
                path.unlink(missing_ok=True)
        return found

    def write(self, name: str, record: ProcessRecord) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        tmp = self.path / f"{name}.json.tmp"
        tmp.write_text(json.dumps(asdict(record), indent=2), encoding="utf-8")
        tmp.replace(self.record_path(name))

    def remove(self, name: str) -> None:
        """Forget a process that has exited: its record and its stop file."""
        self.record_path(name).unlink(missing_ok=True)
        self.stop_path(name).unlink(missing_ok=True)

    def request_stop(self, name: str) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        self.stop_path(name).touch()


def find_process(record: ProcessRecord) -> psutil.Process | None:
    """Return the record's process if its pid is alive and started when recorded, else None."""
    try:
        proc = psutil.Process(record.pid)
        if abs(proc.create_time() - record.started) > START_TIME_TOLERANCE_S:
            return None
        if proc.status() == psutil.STATUS_ZOMBIE:
            return None
    except psutil.Error:
        return None
    return proc


@dataclass
class Child:
    spec: ChildSpec
    proc: psutil.Process | None  # None for a child that exited before it could be looked up
    popen: subprocess.Popen[bytes] | None = None  # None for a process adopted from an earlier `fact dev`

    @property
    def name(self) -> str:
        return self.spec.name

    def exited(self) -> bool:
        if self.popen is not None:
            return self.popen.poll() is not None
        if self.proc is None:
            return True
        try:
            return not self.proc.is_running() or self.proc.status() == psutil.STATUS_ZOMBIE
        except psutil.Error:
            return True

    def exit_code(self) -> int | None:
        """Return the exit code of a child this `fact dev` started (signal deaths as in shells); None if unknown."""
        code = None if self.popen is None else self.popen.returncode
        return 128 - code if code is not None and code < 0 else code


def _inherited(unset: tuple[str, ...]) -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in unset}


def _popen(argv: list[str], env: dict[str, str], log: Path) -> subprocess.Popen[bytes]:
    with log.open("ab") as out:  # the child keeps its own handle; append keeps the last crash's output
        kwargs: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": out,
            "stderr": subprocess.STDOUT,
            "env": env,
        }
        if not IS_WINDOWS:
            return subprocess.Popen(argv, start_new_session=True, **kwargs)  # noqa: S603
        # No console window, no Ctrl-C from ours; leave a job (`uv run`'s) that would kill it with us, if allowed.
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        try:
            breakaway = flags | subprocess.CREATE_BREAKAWAY_FROM_JOB
            return subprocess.Popen(argv, creationflags=breakaway, **kwargs)  # noqa: S603
        except PermissionError:  # the job forbids breaking away
            sys.stderr.write(f"warning: {argv[0]} starts inside fact dev's job; killing fact dev will kill it too\n")
            return subprocess.Popen(argv, creationflags=flags, **kwargs)  # noqa: S603


def start(spec: ChildSpec, run_dir: RunDir) -> Child:
    """Start `spec` detached and record it."""
    run_dir.path.mkdir(parents=True, exist_ok=True)
    run_dir.stop_path(spec.name).unlink(missing_ok=True)  # a stop file left over would stop it at once
    env = {**_inherited(spec.unset), "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", **spec.env}
    if spec.graceful:
        env[STOP_FILE_VAR] = str(run_dir.stop_path(spec.name))
    popen = _popen(spec.argv, env, run_dir.log_path(spec.name))
    record = ProcessRecord(spec.name, spec.build, popen.pid, time.time(), spec.port, list(spec.argv))
    run_dir.write(spec.name, record)  # at once: a `fact dev` dying now still leaves its child recorded
    proc: psutil.Process | None = None
    try:
        proc = psutil.Process(popen.pid)
        run_dir.write(spec.name, replace(record, started=proc.create_time()))
    except psutil.Error:  # exited already; the supervisor sees it on its next poll
        pass
    return Child(spec, proc, popen)


def _wait(child: Child, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while not child.exited():
        if time.monotonic() > deadline:
            return False
        time.sleep(POLL_S)
    return True


def kill_tree(proc: psutil.Process, timeout: float = KILL_TIMEOUT_S) -> None:
    """Kill `proc` and every process below it."""
    try:
        procs = [*proc.children(recursive=True), proc]
    except psutil.Error:
        return
    for p in procs:
        with contextlib.suppress(psutil.Error):
            p.kill()
    psutil.wait_procs(procs, timeout=timeout)


def stop(child: Child, run_dir: RunDir, timeout: float | None = None) -> None:
    """Stop `child` (its stop file, then its tree killed after `timeout`, `STOP_TIMEOUT_S`) and remove its record."""
    if child.spec.graceful and not child.exited():
        run_dir.request_stop(child.name)
        _wait(child, STOP_TIMEOUT_S if timeout is None else timeout)
    if not child.exited() and child.proc is not None:
        kill_tree(child.proc)
    if child.popen is not None:
        child.popen.wait()
    run_dir.remove(child.name)
