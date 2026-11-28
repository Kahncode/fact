"""
`fact dev`'s loop: make the processes running match the specs, each detached and tracked in `<data_dir>/run/`.

At start it adopts every recorded process still alive (same pid and start time) whose command matches its spec,
stops a live one whose command differs (another build), and removes the records of the others. Then, every poll, it
starts what is missing (once `wait_for` accepts connections), starts again a child that exited (after
`RESTART_DELAY_S`) and prints each child's log prefixed by its name. Killing `fact dev` leaves the children running
for the next one to adopt. Ctrl-C, a stop file it did not write appearing for one of its children (`fact shutdown`),
or a restarter raising `StopRequestedError` stops them all, last spec first; runs stay open in Temporal.
"""

import contextlib
import signal
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

from fact.cli.processes import Child, ChildSpec, RunDir, find_process, start, stop

IS_WINDOWS = sys.platform == "win32"
POLL_S = 0.2
RESTART_DELAY_S = 5.0
PUMP_JOIN_S = 2.0
INTERRUPTED = 130  # exit code for Ctrl-C, as in shells
# The console's stop keys; anything else (a kill, a closed terminal) ends `fact dev` alone, its children kept.
STOP_SIGNALS = ("SIGINT", "SIGBREAK") if IS_WINDOWS else ("SIGINT",)

_print_lock = threading.Lock()


class StopRequestedError(Exception):
    """Raised to stop every child and return 0; its message says why."""


# Called on every poll; returns the specs of children to restart (matched by name), or None to keep them running.
# It raises `StopRequestedError` to stop them all.
Restarter = Callable[[], Sequence[ChildSpec] | None]


def emit(name: str, width: int, line: str) -> None:
    with _print_lock:
        sys.stdout.write(f"{name:<{width}} | {line.rstrip()}\n")
        sys.stdout.flush()


class LogTail(threading.Thread):
    """Print the lines a child appends to its log, from `offset`, until `finish` and the end of the file."""

    def __init__(self, name: str, width: int, path: Path, offset: int) -> None:
        super().__init__(name=f"tail-{name}", daemon=True)
        self._child, self._width, self._path, self._offset = name, width, path, offset
        self._done = threading.Event()

    def run(self) -> None:
        try:
            log = self._path.open("rb")
        except OSError:  # an adopted child whose log was deleted
            return
        with log:
            log.seek(self._offset)
            pending = b""
            while True:
                if chunk := log.read(65536):
                    *lines, pending = (pending + chunk).split(b"\n")
                    for line in lines:
                        emit(self._child, self._width, line.decode("utf-8", "replace"))
                elif self._done.is_set():
                    break
                else:
                    time.sleep(POLL_S)
            if pending:
                emit(self._child, self._width, pending.decode("utf-8", "replace"))

    def finish(self) -> None:
        self._done.set()
        self.join(timeout=PUMP_JOIN_S)


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


class Supervisor:
    def __init__(self, specs: Sequence[ChildSpec], run_dir: RunDir) -> None:
        self.specs = {s.name: s for s in specs}  # start order
        self.run_dir = run_dir
        self.children: dict[str, Child] = {}
        self._tails: dict[str, LogTail] = {}
        self._not_before: dict[str, float] = {}
        self.width = max(len(n) for n in [*self.specs, "fact"])

    def log(self, line: str) -> None:
        emit("fact", self.width, line)

    def adopt(self) -> None:
        """Take over the live recorded processes that match their spec; stop or forget the others."""
        for name, record in self.run_dir.records().items():
            proc = find_process(record)
            spec = self.specs.get(name)
            if proc is None:
                self.run_dir.remove(name)
                continue
            if spec is None:
                self.log(f"{name} (pid {record.pid}) is recorded but not one of fact dev's processes; leaving it")
                continue
            child = Child(spec, proc)
            if record.command != spec.argv:
                self.log(f"{name} (pid {record.pid}) runs build {record.build}, not {spec.build}: stopping it")
                stop(child, self.run_dir)
                continue
            self.log(f"adopted {name} (pid {record.pid}, build {record.build})")
            self._track(child, offset=self._log_size(name))

    def _log_size(self, name: str) -> int:
        path = self.run_dir.log_path(name)
        return path.stat().st_size if path.exists() else 0

    def _track(self, child: Child, offset: int) -> None:
        self.children[child.name] = child
        tail = LogTail(child.name, self.width, self.run_dir.log_path(child.name), offset)
        tail.start()
        self._tails[child.name] = tail

    def _forget(self, name: str) -> None:
        self.children.pop(name, None)
        if (tail := self._tails.pop(name, None)) is not None:
            tail.finish()

    def start_missing(self) -> None:
        now = time.monotonic()
        for name, spec in self.specs.items():
            if name in self.children or now < self._not_before.get(name, 0.0):
                continue
            if spec.wait_for is not None and not port_open(*spec.wait_for):
                continue
            offset = self._log_size(name)
            self._track(start(spec, self.run_dir), offset)

    def reap(self) -> None:
        """Forget the children that exited, to be started again after `RESTART_DELAY_S`."""
        for name, child in list(self.children.items()):
            if not child.exited():
                continue
            stop(child, self.run_dir)  # reaps it and removes its record
            self._forget(name)
            code = child.exit_code()
            how = f"exited with code {code}" if code is not None else "exited"
            self.log(f"{name} {how}; starting it again in {RESTART_DELAY_S:g} s")
            self._not_before[name] = time.monotonic() + RESTART_DELAY_S

    def replace(self, specs: Sequence[ChildSpec]) -> None:
        """Stop the children named in `specs` and run `specs` in their place; the others keep running."""
        for spec in specs:
            if (child := self.children.get(spec.name)) is not None:
                stop(child, self.run_dir)
                self._forget(spec.name)
            self.specs[spec.name] = spec
            self._not_before.pop(spec.name, None)

    def stop_all(self) -> None:
        for name in reversed(self.specs):
            if (child := self.children.get(name)) is not None:
                self.log(f"stopping {name}")
                stop(child, self.run_dir)
                self._forget(name)

    def check_stop_files(self) -> None:
        """Raise `StopRequestedError` once a child's stop file appears: `stop` removes its own before returning."""
        if asked := [name for name in self.children if self.run_dir.stop_path(name).exists()]:
            msg = f"stop requested for {', '.join(asked)} (fact shutdown): stopping every process"
            raise StopRequestedError(msg)

    def loop(self, restarter: Restarter | None) -> None:
        while True:
            self.check_stop_files()  # before `reap`, so a child that stopped on request is not started again
            self.reap()
            self.start_missing()
            if restarter is not None and (replacements := restarter()):
                self.replace(replacements)
            time.sleep(POLL_S)


def _raise_interrupt(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


@contextlib.contextmanager
def _stop_signals(handler: object) -> Iterator[None]:
    """Install `handler` for every stop signal, restoring the previous handlers on exit."""
    sigs = [getattr(signal, name) for name in STOP_SIGNALS]
    previous = {sig: signal.signal(sig, handler) for sig in sigs}
    try:
        yield
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)


def run_all(specs: Sequence[ChildSpec], run_dir: RunDir, restarter: Restarter | None = None) -> int:
    """
    Adopt or start `specs` and keep them running until Ctrl-C (130) or a stop request (0), then stop them.

    `restarter`, polled every loop, may return specs to restart some children with (`fact dev --follow`).
    """
    supervisor = Supervisor(specs, run_dir)
    code = 0
    try:
        with _stop_signals(_raise_interrupt):
            supervisor.adopt()
            supervisor.loop(restarter)
    except KeyboardInterrupt:
        code = INTERRUPTED
    except StopRequestedError as e:
        supervisor.log(str(e))
    with _stop_signals(signal.SIG_IGN):  # a second Ctrl-C must not abandon the stop half way
        supervisor.stop_all()
    supervisor.log("stopped; open runs carry on at the next `fact dev`")
    return code
