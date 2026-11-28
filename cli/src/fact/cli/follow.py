"""
`fact dev --follow <ref>`: upgrade the running factory whenever `<ref>` moves, once no run is active.

Every `CHECK_INTERVAL_S` the follower fetches `origin` and resolves `<ref>`. When it differs from the pinned SHA, an
upgrade is pending: the scheduler is turned off (`POST /scheduler/off`) so no new run starts, and the follower waits
until `GET /scheduler/active` counts no run (queued, running or waiting, human waits included). Then it pins `<ref>`
(`fact use`) and hands the supervisor the API and worker commands for the new checkout; Temporal keeps running.
The API comes back with its scheduler on; if it was off before the upgrade, the follower turns it off again.

A failed check (git, `uv sync`, the API) is printed and retried at the next check.
"""

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from fact.cli import projects
from fact.cli.processes import ChildSpec
from fact.cli.runs import API
from fact.cli.use import PinError, latest, pin
from fact.core.settings import REPO_ROOT, Settings

CHECK_INTERVAL_S = 60.0
# The restarted API starts with its scheduler on and ticks first after `scheduler_interval_seconds` (30); retrying
# every second turns it back off well before that.
RESTORE_INTERVAL_S = 1.0
# what a check can raise: git or uv failing (PinError), the API down or answering oddly, a file lock while pinning
CHECK_ERRORS = (PinError, httpx.HTTPError, KeyError, TypeError, ValueError, OSError)


def call_scheduler(method: str, path: str) -> dict[str, Any]:
    """Call `/api/v1/scheduler<path>`; errors are raised for the caller to retry at its next check."""
    with projects.make_client() as client:
        response = client.request(method, f"{API}/scheduler{path}")
    response.raise_for_status()
    return response.json()


def short(sha: str) -> str:
    return sha[:12]


class Follower:
    """Called by the supervisor on every poll; returns the commands to restart with once an upgrade is done."""

    def __init__(  # noqa: PLR0913
        self,
        settings: Settings,
        ref: str,
        *,
        uv: str,
        pinned: str,
        commands: Callable[[Path], list[ChildSpec]],
        log: Callable[[str], None],
        repo: Path = REPO_ROOT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings, self._ref, self._uv, self._repo = settings, ref, uv, repo
        self._commands, self._log, self._clock = commands, log, clock
        self.pinned = pinned
        self.pending: str | None = None  # the SHA `ref` moved to, while the upgrade waits for runs to end
        self._resume_on = True
        self._restore_at: float | None = None  # when to try turning the scheduler back off after a restart
        self._active: int | None = None
        self._next = clock() + CHECK_INTERVAL_S

    def __call__(self) -> list[ChildSpec] | None:
        now = self._clock()
        if self._restore_at is not None and now >= self._restore_at:
            self._restore_off(now)
        if now < self._next:
            return None
        self._next = now + CHECK_INTERVAL_S
        try:
            return self._check()
        except CHECK_ERRORS as exc:
            self._log(f"upgrade check failed: {exc}; retrying in {CHECK_INTERVAL_S:g} s")
            return None

    def _check(self) -> list[ChildSpec] | None:
        if self.pending is None:
            sha = latest(self._ref, self._repo)
            if sha == self.pinned:
                return None
            self._resume_on = bool(call_scheduler("GET", "")["on"])
            call_scheduler("POST", "/off")
            self.pending = sha
            self._log(f"{self._ref} moved {short(self.pinned)} -> {short(sha)}: upgrade pending, no new run starts")
        active = int(call_scheduler("GET", "/active")["active"])
        if active:
            if active != self._active:
                self._log(f"upgrade waits for {active} active run(s) to end")
            self._active = active
            return None
        self._log(f"no run active: upgrading to {self._ref}")
        checkout = pin(self._settings, self._ref, self._uv, self._repo)
        self.pinned, self.pending, self._active = checkout.name, None, None
        self._restore_at = None if self._resume_on else self._clock()
        self._log(f"restarting the API and worker on {short(checkout.name)}")
        return self._commands(checkout)

    def _restore_off(self, now: float) -> None:
        """Turn the scheduler back off once the restarted API answers; it is not up yet on the first tries."""
        self._restore_at = now + RESTORE_INTERVAL_S
        try:
            call_scheduler("POST", "/off")
        except CHECK_ERRORS:  # not up yet
            return
        self._restore_at = None
        self._log("scheduler kept off, as it was before the upgrade")
