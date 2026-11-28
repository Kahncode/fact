"""
`fact dev --pinned` and `--follow` run their own CLI from the newest pin, so the follower's code is never stale.

The `fact dev` a person starts is a launcher (`launch`): it runs `fact dev --in-pin` from the current pin's `.venv`
and waits for it; that one supervises. When the pin moves to one whose CLI differs (`cli/` or `core/` changed,
`SelfRestart`), the supervising `fact dev` exits with `RESTART`, leaving every process running, and the launcher runs
`fact dev` again from the new pin, which adopts them through their records (`fact.cli.processes`): Temporal, the API
and the workers are not restarted. A pin older than this module cannot be run so: the launcher supervises itself, and
restarts the same way once a pin that can is current. A supervising `fact dev` whose launcher is gone exits too,
its processes left running, as killing `fact dev` always did.
"""

import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path

import psutil
from fact.cli.supervisor import Changes, DetachError, ignore_stop_signals
from fact.cli.use import PinError, changed, current_pin
from fact.core.settings import Settings

RESTART = 75  # the supervising `fact dev`'s exit code asking its launcher to run it again from the current pin
MARKER = Path("cli/src/fact/cli/relaunch.py")  # a pin holding it can be run with `--in-pin`
CLI_PATHS = ["cli/", "core/"]  # the code `fact dev` runs
CHECK_INTERVAL_S = 5.0


def runs_in_pin(checkout: Path) -> bool:
    """Return whether `checkout`'s CLI can run `fact dev --in-pin` (it holds this module)."""
    return (checkout / MARKER).is_file()


def launch(
    settings: Settings,
    child_argv: Callable[[Path], list[str]],
    env: Mapping[str, str],
    supervise: Callable[[], int],
) -> int:
    """Run `fact dev` from the current pin (`child_argv`), or `supervise` here for an older pin, until no `RESTART`."""
    while True:
        checkout = current_pin(settings)
        if runs_in_pin(checkout):
            with ignore_stop_signals():  # the child shares the console: it handles Ctrl-C and exits
                code = subprocess.run(child_argv(checkout), env=dict(env), check=False).returncode  # noqa: S603
        else:
            code = supervise()
        if code != RESTART:
            return code


def _process(pid: int | None) -> psutil.Process | None:
    if pid is None:
        return None
    try:
        return psutil.Process(pid)
    except psutil.Error:
        return None


class SelfRestart:
    """Restarter: raise `DetachError(RESTART)` once the current pin's CLI is not the one running, or 0 alone."""

    def __init__(  # noqa: PLR0913
        self,
        settings: Settings,
        *,
        running: str | None,
        repo: Path,
        log: Callable[[str], None],
        launcher: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings, self._repo, self._log, self._clock = settings, repo, log, clock
        self._running = running  # the pin this CLI runs from; None from the source tree
        self._checked = running  # the last pin compared with it
        self._watch = launcher is not None
        self._launcher = _process(launcher)
        self._next = 0.0

    def __call__(self) -> Changes | None:
        now = self._clock()
        if now < self._next:
            return None
        self._next = now + CHECK_INTERVAL_S
        if self._watch and (self._launcher is None or not self._launcher.is_running()):
            msg = "the fact dev that launched this one is gone: exiting"
            raise DetachError(msg, 0)
        try:
            checkout = current_pin(self._settings)
        except PinError:
            return None
        pin = checkout.name
        if pin == self._checked:
            return None
        self._checked = pin
        if self._running is None and not runs_in_pin(checkout):
            return None  # from the source tree: only a pin that can run `--in-pin` is worth restarting for
        if self._running is not None and not changed(self._repo, self._running, pin, CLI_PATHS):
            return None
        msg = f"pinned {pin[:12]} runs another CLI: restarting fact dev from it"
        raise DetachError(msg, RESTART)
