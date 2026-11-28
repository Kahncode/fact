"""
`fact shutdown`: stop the factory's processes, not its runs, such as before a reboot.

It acts on the records in `<data_dir>/run/` (`fact.cli.processes`), so it works whether or not `fact dev` runs: it
writes the stop file of every worker and waits for them to exit, then the API's (and any other recorded process's),
then stops Temporal, last. A process still alive after `STOP_TIMEOUT_S` has its tree killed. A worker's graceful stop
ends a running agent step `AGENT_INTERRUPTED`, so the next `fact dev` resumes its session; runs stay open in Temporal.
A running `fact dev` sees a stop file it did not write, or a child exited with its record removed, and stops
everything itself, then exits 0.
"""

from collections.abc import Callable

import psutil
import typer
from fact.cli.dev import run_dir
from fact.cli.processes import STOP_TIMEOUT_S, ProcessRecord, RunDir, find_process, kill_tree
from fact.core.settings import get_settings

TEMPORAL = "temporal"
WORKER = "worker"


def _phase(name: str, record: ProcessRecord) -> int:
    """Stop order: the workers first, then the API and the rest, Temporal last."""
    if TEMPORAL in (record.role, name):
        return 2
    return 0 if record.role.startswith(WORKER) else 1


def _stop_phase(run_dir: RunDir, procs: dict[str, psutil.Process], timeout: float) -> None:
    for name in procs:
        run_dir.request_stop(name)
    _, alive = psutil.wait_procs(list(procs.values()), timeout=timeout)
    for proc in alive:
        kill_tree(proc)
    for name in procs:
        run_dir.remove(name)


def stop_recorded(
    run_dir: RunDir, log: Callable[[str], None], timeout: float = STOP_TIMEOUT_S
) -> list[tuple[str, int]]:
    """Stop every recorded live process in order and remove its record; return the (name, pid) of those stopped."""
    phases: list[dict[str, psutil.Process]] = [{}, {}, {}]
    for name, record in run_dir.records().items():
        proc = find_process(record)
        if proc is None:
            run_dir.remove(name)
            continue
        phases[_phase(name, record)][name] = proc
    workers, rest, temporal = phases
    stopped: list[tuple[str, int]] = []
    for group in (workers, rest):
        if group:
            log(f"stopping {', '.join(group)}")
            _stop_phase(run_dir, group, timeout)
            stopped += [(n, p.pid) for n, p in group.items()]
    if temporal:  # it watches no stop file: killed once everything that calls it has exited
        log(f"stopping {', '.join(temporal)}")
        _stop_phase(run_dir, temporal, timeout=0)
        stopped += [(n, p.pid) for n, p in temporal.items()]
    return stopped


def shutdown() -> None:
    """Stop the workers, the API and Temporal now; open runs carry on at the next `fact dev`."""
    stopped = stop_recorded(run_dir(get_settings()), typer.echo)
    if not stopped:
        typer.echo("No factory process is running; nothing to stop.")
        return
    typer.echo("Stopped " + ", ".join(f"{name} (pid {pid})" for name, pid in stopped) + ".")
    typer.echo("Open runs stay open in Temporal and carry on at the next `fact dev`.")
