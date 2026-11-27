"""Run several child processes together: prefixed output, and all stop when one exits or on Ctrl-C."""

import contextlib
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import IO

IS_WINDOWS = sys.platform == "win32"
STOP_TIMEOUT_S = 10.0
PORT_TIMEOUT_S = 60.0
POLL_S = 0.2
PUMP_JOIN_S = 2.0
INTERRUPTED = 130  # exit code for Ctrl-C, as in shells
# Signals that stop `fact dev` itself; each must stop the children too, since they don't share our session/group.
STOP_SIGNALS = ("SIGINT", "SIGTERM", "SIGBREAK") if IS_WINDOWS else ("SIGINT", "SIGTERM", "SIGHUP")

_print_lock = threading.Lock()


@dataclass(frozen=True)
class ChildSpec:
    name: str
    argv: list[str]
    wait_for: tuple[str, int] | None = None  # (host, port) that must accept connections before starting
    env: dict[str, str] = field(default_factory=dict)  # added to this process's environment
    unset: tuple[str, ...] = ()  # removed from this process's environment before `env` is added


@dataclass(frozen=True)
class Child:
    name: str
    proc: subprocess.Popen[str]


class PortTimeoutError(RuntimeError):
    pass


class StopRequestedError(Exception):
    """Raised by a restarter to stop every child and return 0; its message says why (`fact shutdown`)."""


def emit(name: str, width: int, line: str) -> None:
    with _print_lock:
        sys.stdout.write(f"{name:<{width}} | {line.rstrip()}\n")
        sys.stdout.flush()


def _pump(name: str, width: int, stream: IO[str]) -> None:
    for line in stream:
        emit(name, width, line)


def _inherited(unset: tuple[str, ...]) -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in unset}


def _start(spec: ChildSpec, width: int, children: list[Child], pumps: list[threading.Thread]) -> None:
    # Own process group (Windows) or session (POSIX): the console's Ctrl-C reaches only us, and we stop them in order.
    kwargs: dict = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WINDOWS else {"start_new_session": True}
    proc = subprocess.Popen(  # noqa: S603
        spec.argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**_inherited(spec.unset), "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", **spec.env},
        **kwargs,
    )
    children.append(Child(spec.name, proc))  # tracked before anything else can be interrupted
    assert proc.stdout is not None  # noqa: S101
    pump = threading.Thread(target=_pump, args=(spec.name, width, proc.stdout), daemon=True)
    pump.start()
    pumps.append(pump)


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def _wait_for_port(host: str, port: int, children: Sequence[Child], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not _port_open(host, port):
        if exited := first_exit(children):
            msg = f"{exited[0]} exited while waiting for {host}:{port}"
            raise PortTimeoutError(msg)
        if time.monotonic() > deadline:
            msg = f"{host}:{port} did not open within {timeout:.0f}s"
            raise PortTimeoutError(msg)
        time.sleep(POLL_S)


def _signal(proc: subprocess.Popen[str], step: str) -> None:
    """Send `step` (interrupt, terminate, kill); on POSIX to the child's whole process group."""
    if IS_WINDOWS:
        with contextlib.suppress(OSError):  # already exited
            if step == "interrupt":
                proc.send_signal(signal.CTRL_BREAK_EVENT)  # reaches the child's whole group
            elif step == "terminate":
                proc.terminate()
            else:
                proc.kill()
        return
    sig = {"interrupt": signal.SIGINT, "terminate": signal.SIGTERM}.get(step, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, sig)


def _stop_one(proc: subprocess.Popen[str], timeout: float) -> None:
    for step in ("interrupt", "terminate", "kill"):
        if proc.poll() is not None:
            return
        _signal(proc, step)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=timeout)


def stop_all(children: Sequence[Child], timeout: float = STOP_TIMEOUT_S) -> None:
    """Stop children one at a time, last started first, escalating from interrupt to terminate to kill."""
    for child in reversed(children):
        _stop_one(child.proc, timeout)
        child.proc.wait()


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


def first_exit(children: Sequence[Child]) -> tuple[str, int] | None:
    for child in children:
        if (rc := child.proc.poll()) is not None:
            return child.name, (128 - rc if rc < 0 else rc)  # POSIX: -N means killed by signal N
    return None


# Called on every poll; returns the specs of children to restart (matched by name), or None to keep them running.
# It raises `StopRequestedError` to stop them all.
Restarter = Callable[[], Sequence[ChildSpec] | None]


def _start_all(specs: Sequence[ChildSpec], width: int, children: list[Child], pumps: list[threading.Thread]) -> None:
    for spec in specs:
        if spec.wait_for:
            _wait_for_port(*spec.wait_for, children, PORT_TIMEOUT_S)
        _start(spec, width, children, pumps)


def _restart(specs: Sequence[ChildSpec], width: int, children: list[Child], pumps: list[threading.Thread]) -> None:
    """Stop the children named in `specs`, then start `specs` in their place; the others keep running."""
    names = {s.name for s in specs}
    stop_all([c for c in children if c.name in names])
    children[:] = [c for c in children if c.name not in names]
    _start_all(specs, width, children, pumps)


def _supervise(
    specs: Sequence[ChildSpec],
    width: int,
    children: list[Child],
    pumps: list[threading.Thread],
    restarter: Restarter | None,
) -> int:
    _start_all(specs, width, children, pumps)
    while (exited := first_exit(children)) is None:
        if restarter is not None and (replacements := restarter()):
            _restart(replacements, width, children, pumps)
        time.sleep(POLL_S)
    name, code = exited
    emit("fact", width, f"{name} exited with code {code}; stopping the others")
    return code


def run_all(specs: Sequence[ChildSpec], restarter: Restarter | None = None) -> int:
    """
    Start `specs` in order and block until one exits or Ctrl-C; return the first exit code (130 on Ctrl-C).

    `restarter`, polled while all run, may return specs to restart some children with (`fact dev --follow`), or raise
    `StopRequestedError` to stop them all and return 0 (`fact shutdown`).
    """
    width = max(len(s.name) for s in [*specs, ChildSpec("fact", [])])
    children: list[Child] = []
    pumps: list[threading.Thread] = []
    code = 0
    try:
        with _stop_signals(_raise_interrupt):
            code = _supervise(specs, width, children, pumps, restarter)
    except KeyboardInterrupt:
        code = INTERRUPTED
    except StopRequestedError as e:
        emit("fact", width, str(e))
        code = 0
    except PortTimeoutError as e:
        emit("fact", width, f"error: {e}")
        code = 1
    finally:
        with _stop_signals(signal.SIG_IGN):  # a second Ctrl-C must not abandon the children
            stop_all(children)
        for pump in pumps:
            pump.join(timeout=PUMP_JOIN_S)  # flush the last lines
    return code
