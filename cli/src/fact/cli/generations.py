"""
`fact dev --pinned`'s worker generations: one worker process per generation that should run.

A generation (`fact.core.generations`) is a factory pin with each project's home SHA, recorded under
`<data_dir>/run/generations/`. What should run: the newest generation, plus every older one with a version Temporal
still needs a worker for (`fact.cli.drainage`: current, ramping or `DRAINING`). Every `CHECK_INTERVAL_S`:

- when `pinned/current` names another pin than the newest generation's (`fact use`, or the follower), a new
  generation is recorded, the API restarts from the new pin and the new generation's worker starts next to the old
  ones; its worker makes its versions current;
- an older generation that should run and has no worker gets one, from its own pin (after a reboot, say);
- an older generation whose versions are all drained has its worker stopped gracefully.

While Temporal cannot be asked, no older generation is started or stopped.
"""

import time
from collections.abc import Callable, Sequence
from pathlib import Path

from fact.cli.drainage import Version
from fact.cli.processes import ChildSpec, RunDir, find_process
from fact.cli.supervisor import Changes
from fact.cli.use import PinError, current_pin, factory_checkout
from fact.core import generations
from fact.core.generations import Generation
from fact.core.settings import Settings

CHECK_INTERVAL_S = 15.0
WORKER_PREFIX = "worker-"

WorkerSpec = Callable[[Path, Generation], ChildSpec]  # (its pin's checkout, generation) to the worker's command
ApiSpec = Callable[[Path], ChildSpec]  # a pin's checkout to the API's command
InUse = Callable[[Sequence[Version]], set[Version] | None]


def worker_name(generation: Generation) -> str:
    return f"{WORKER_PREFIX}{generation.id}"


class Generations:
    """Called by the supervisor on every poll; returns the workers (and API) to start or stop."""

    def __init__(  # noqa: PLR0913
        self,
        settings: Settings,
        *,
        worker_spec: WorkerSpec,
        api_spec: ApiSpec,
        in_use: InUse,
        ready: Callable[[Path], bool],
        log: Callable[[str], None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings, self._worker_spec, self._api_spec = settings, worker_spec, api_spec
        self._in_use, self._ready, self._log, self._clock = in_use, ready, log, clock
        self.directory = generations.generations_dir(settings.data_dir)
        self.running: set[str] = set()  # ids of the generations with a worker spec in the supervisor
        self._next = 0.0
        self._missing: set[str] = set()  # pins already reported missing

    def newest(self, checkout: Path) -> Generation:
        """Return the newest generation, first recording one for `checkout` when the newest is of another pin."""
        latest = generations.newest(self.directory)
        if latest is not None and latest.factory == checkout.name:
            return latest
        return generations.create(self.directory, checkout.name)

    def initial(self, checkout: Path, run_dir: RunDir) -> list[ChildSpec]:
        """Return the workers to run at start: the newest generation's, and each older one's still running (to adopt)."""
        newest = self.newest(checkout)
        records = run_dir.records()
        specs = [self._worker_spec(checkout, newest)]
        self.running = {newest.id}
        for generation in generations.all_generations(self.directory):
            record = records.get(worker_name(generation))
            if generation.id == newest.id or record is None or find_process(record) is None:
                continue
            if (spec := self._spec(generation)) is not None:
                specs.append(spec)
                self.running.add(generation.id)
        return specs

    def __call__(self) -> Changes | None:
        now = self._clock()
        if now < self._next:
            return None
        self._next = now + CHECK_INTERVAL_S
        start: list[ChildSpec] = []
        self._upgrade(start)
        stop = self._drain(start)
        changes = Changes(tuple(start), tuple(stop))
        return changes or None

    def _spec(self, generation: Generation) -> ChildSpec | None:
        checkout = factory_checkout(self._settings, generation.factory)
        if self._ready(checkout):
            return self._worker_spec(checkout, generation)
        if generation.id not in self._missing:
            self._missing.add(generation.id)
            self._log(f"generation {generation.id}: its pin {checkout} is missing or has no .venv; not starting it")
        return None

    def _upgrade(self, start: list[ChildSpec]) -> None:
        """Start a new generation (and the API from its pin) once `pinned/current` names another pin."""
        try:
            checkout = current_pin(self._settings)
        except PinError:
            return
        latest = generations.newest(self.directory)
        if latest is not None and latest.factory == checkout.name:
            return
        if not self._ready(checkout):
            return  # `fact use` writes `current` once the pin is synced; a missing venv is reported at its start
        generation = generations.create(self.directory, checkout.name)
        self._log(f"pinned {checkout.name[:12]}: restarting the API and starting generation {generation.id}")
        start += [self._api_spec(checkout), self._worker_spec(checkout, generation)]
        self.running.add(generation.id)

    def _drain(self, start: list[ChildSpec]) -> list[str]:
        """Start the older generations that should run and are not; return the workers to stop."""
        found = generations.all_generations(self.directory)
        older = found[:-1]
        versions = [v for g in older for v in g.versions()]
        in_use = self._in_use(versions)
        if in_use is None:
            return []
        stop: list[str] = []
        for generation in older:
            needed = any(v in in_use for v in generation.versions())
            if needed and generation.id not in self.running and (spec := self._spec(generation)) is not None:
                self._log(f"generation {generation.id} still has runs: starting its worker")
                start.append(spec)
                self.running.add(generation.id)
            elif not needed and generation.id in self.running:
                self._log(f"generation {generation.id} has no run left: stopping its worker")
                stop.append(worker_name(generation))
                self.running.discard(generation.id)
        return stop
