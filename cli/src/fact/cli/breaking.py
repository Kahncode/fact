"""
A breaking upgrade (`fact.core.migration_rules`): stop the open runs, upgrade, then start their tasks again.

The follower (`fact.cli.follow`) starts one when a migration between the running pin's head and the new pin's head
says `breaking = True`, with the new pin prepared but not current. `BreakingUpgrade`, a restarter polled by the
supervisor, then moves it on every `CHECK_INTERVAL_S`, each phase recorded in `<data_dir>/run/upgrade.json` so a
`fact dev` that starts with the file finishes it:

1. `stopping`: turn the scheduler off (its state before is recorded first), record the open runs and their tasks,
   `fact stop` each (reason `breaking upgrade`) and wait until each shows `stopped`;
2. `upgrading`: make the new pin current and start its generation (the API restarts from it and migrates); once its
   worker is through its start, stop the older generations;
3. `restarting`: start each recorded task again as a manual start (which releases its hold; the new run gets the
   stopped run's handoff), restore the scheduler's state, delete the file.

An API that cannot be reached (it restarts) is retried at the next check. A run that will not stop keeps the upgrade
in `stopping`: `fact stop --force` it by hand.
"""

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from fact.cli.generations import Generations
from fact.cli.processes import UPGRADE_FILE
from fact.cli.supervisor import Changes
from fact.cli.upgrade_api import HttpUpgradeApi, OpenRun, UpgradeApi, UpgradeApiError
from fact.cli.use import PinError, current_pin, factory_checkout, set_current
from fact.core import generations
from fact.core.settings import Settings

CHECK_INTERVAL_S = 5.0
REASON = "breaking upgrade"
STOPPING, UPGRADING, RESTARTING = "stopping", "upgrading", "restarting"


@dataclass
class UpgradeState:
    pin: str  # the new factory SHA
    scheduler_was_on: bool
    phase: str = STOPPING
    runs: list[OpenRun] | None = None  # None until recorded, with the scheduler off
    restarted: list[str] = field(default_factory=list)  # ids of the recorded runs whose task started again


def upgrade_path(settings: Settings) -> Path:
    return settings.data_dir / "run" / UPGRADE_FILE


def read_state(path: Path) -> UpgradeState | None:
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    runs = data.pop("runs", None)
    return UpgradeState(**data, runs=None if runs is None else [OpenRun(**r) for r in runs])


def write_state(path: Path, state: UpgradeState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8", newline="\n")
    tmp.replace(path)


class BreakingUpgrade:
    """Restarter: moves the breaking upgrade recorded in `upgrade.json` on; idle while there is none."""

    def __init__(
        self,
        settings: Settings,
        *,
        workers: Generations,
        log: Callable[[str], None],
        api: UpgradeApi | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings, self._workers, self._log, self._clock = settings, workers, log, clock
        self._api = api or HttpUpgradeApi()
        self.path = upgrade_path(settings)
        self._asked: set[str] = set()  # runs asked to stop by this process
        self._next = 0.0

    @property
    def active(self) -> bool:
        return self.path.is_file()

    def begin(self, pin: str, reasons: list[str]) -> None:
        """Record a breaking upgrade to `pin`, its checkout prepared; the next polls carry it out."""
        self._log(f"upgrade to {pin[:12]} is breaking ({', '.join(reasons)}): stopping the open runs first")
        write_state(self.path, UpgradeState(pin=pin, scheduler_was_on=self._api.scheduler_on()))
        self._next = 0.0

    def __call__(self) -> Changes | None:
        now = self._clock()
        if now < self._next:
            return None
        self._next = now + CHECK_INTERVAL_S
        state = read_state(self.path)
        if state is None:
            return None
        try:
            return self._advance(state)
        except UpgradeApiError as exc:
            self._log(f"breaking upgrade ({state.phase}): {exc}; retrying in {CHECK_INTERVAL_S:g} s")
            return None

    def _advance(self, state: UpgradeState) -> Changes | None:
        if state.phase == STOPPING and not self._stopped(state):
            return None
        if state.phase in (STOPPING, UPGRADING):
            return self._upgrade(state)
        self._restart(state)
        return None

    def _stopped(self, state: UpgradeState) -> bool:
        """Stop the recorded runs; return whether every one shows `stopped` (moving on to `upgrading`)."""
        if state.runs is None:
            self._api.set_scheduler(on=False)
            state.runs = self._api.open_runs([p.name for p in self._settings.projects])
            write_state(self.path, state)
            self._log(f"scheduler off; stopping {len(state.runs)} open runs: {_keys(state.runs)}")
        waiting = []
        for run in state.runs:
            if run.run_id not in self._asked:
                self._api.stop(run.run_id, REASON)
                self._asked.add(run.run_id)
            if not self._api.finished(run.run_id):
                waiting.append(run)
        if waiting:
            return False
        state.phase = UPGRADING
        write_state(self.path, state)
        self._log("every open run stopped: upgrading")
        return True

    def _upgrade(self, state: UpgradeState) -> Changes | None:
        """Make the pin current and start its generation; once its worker started, stop the older ones."""
        try:
            current = current_pin(self._settings).name
        except PinError:
            current = None
        if current != state.pin:
            set_current(self._settings, state.pin)
            return Changes(self._workers.add(factory_checkout(self._settings, state.pin)))
        newest = generations.newest(self._workers.directory)
        if newest is None or newest.factory != state.pin or not generations.started(self._workers.directory, newest.id):
            return None  # `Generations` starts it again if it failed
        state.phase = RESTARTING
        write_state(self.path, state)
        return Changes(stop=self._workers.retire_older())

    def _restart(self, state: UpgradeState) -> None:
        for run in state.runs or []:
            if run.run_id in state.restarted:
                continue
            refused = self._api.start(run.task_key, run.project)
            self._log(f"{run.task_key}: not started again: {refused}" if refused else f"{run.task_key}: started again")
            state.restarted.append(run.run_id)
            write_state(self.path, state)
        self._api.set_scheduler(on=state.scheduler_was_on)
        self.path.unlink(missing_ok=True)
        self._log(f"breaking upgrade to {state.pin[:12]} done; scheduler {'on' if state.scheduler_was_on else 'off'}")


def _keys(runs: list[OpenRun]) -> str:
    return ", ".join(r.task_key for r in runs) or "none"
