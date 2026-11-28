"""
`fact dev --follow <ref>`: upgrade the running factory whenever `<ref>` moves, once no run is active.

Every `CHECK_INTERVAL_S` the follower fetches `origin` and resolves `<ref>`. When it differs from the pinned SHA, an
upgrade is pending: the scheduler is turned off (`POST /scheduler/off`) so no new run starts, and the follower waits
until `GET /scheduler/active` counts no run (queued, running or waiting, human waits included). Then it pins `<ref>`
(`fact use`); `fact dev` sees the new pin (`fact.cli.generations`), restarts the API from it and starts its worker
generation next to the old ones; Temporal keeps running.
The scheduler's switch is kept in the database, so the API comes back with it off; if it was on before the upgrade, the
follower turns it back on once the restarted API answers. That it turned the switch off is kept in
`<data_dir>/run/follow-paused` (`PAUSED_NAME`) until it is on again, so a follower restarted while an upgrade is
pending, or before the restarted API answered, still turns it back on.

A failed check (git, `uv sync`, the API) is printed and retried at the next check.
"""

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from fact.cli import projects
from fact.cli.runs import API
from fact.cli.use import PinError, latest, pin
from fact.core.settings import REPO_ROOT, Settings

CHECK_INTERVAL_S = 60.0
# how often to try turning the scheduler back on while the restarted API is not answering yet; each try is one request
# with no retry, so a down API never blocks the supervisor for longer than a refused connection
RESTORE_INTERVAL_S = 1.0
# in `<data_dir>/run/`: present while the follower holds off a scheduler that was on before the upgrade
PAUSED_NAME = "follow-paused"
# what a check can raise: git or uv failing (PinError), the API down or answering oddly, a file lock while pinning
CHECK_ERRORS = (PinError, httpx.HTTPError, KeyError, TypeError, ValueError, OSError)


def call_scheduler(method: str, path: str, *, retry: bool = True) -> dict[str, Any]:
    """
    Call `/api/v1/scheduler<path>`; errors are raised for the caller to retry at its next check.

    With `retry`, the CLI client rides out a few seconds of an API restart; without, it makes a single attempt.
    """
    with projects.make_client() if retry else projects.make_client(None) as client:
        response = client.request(method, f"{API}/scheduler{path}")
    response.raise_for_status()
    return response.json()


def short(sha: str) -> str:
    return sha[:12]


class Follower:
    """Called by the supervisor on every poll; pins `ref` once an upgrade is due (`fact dev` starts the new pin)."""

    def __init__(  # noqa: PLR0913
        self,
        settings: Settings,
        ref: str,
        *,
        uv: str,
        pinned: str,
        log: Callable[[str], None],
        repo: Path = REPO_ROOT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings, self._ref, self._uv, self._repo = settings, ref, uv, repo
        self._log, self._clock = log, clock
        self.pinned = pinned
        self.pending: str | None = None  # the SHA `ref` moved to, while the upgrade waits for runs to end
        self._paused = settings.data_dir / "run" / PAUSED_NAME
        self._restore_at: float | None = None  # when to try turning the scheduler back on after a restart
        self._active: int | None = None
        self._next = clock() + CHECK_INTERVAL_S

    def __call__(self) -> None:
        now = self._clock()
        if self._restore_at is not None and now >= self._restore_at:
            self._restore_on(now)
        if now < self._next:
            return
        self._next = now + CHECK_INTERVAL_S
        try:
            self._check()
        except CHECK_ERRORS as exc:
            self._log(f"upgrade check failed: {exc}; retrying in {CHECK_INTERVAL_S:g} s")

    def _check(self) -> None:
        if self.pending is None:
            sha = latest(self._ref, self._repo)
            if sha == self.pinned:
                if self._paused.exists() and self._restore_at is None:  # restarted before turning it back on
                    self._restore_at = self._clock()
                return
            # a follower restarted while an upgrade was pending finds the switch off by its own doing: the file says so
            if not self._paused.exists() and call_scheduler("GET", "")["on"]:
                self._paused.parent.mkdir(parents=True, exist_ok=True)
                self._paused.touch()
            call_scheduler("POST", "/off")
            self.pending = sha
            self._log(f"{self._ref} moved {short(self.pinned)} -> {short(sha)}: upgrade pending, no new run starts")
        active = int(call_scheduler("GET", "/active")["active"])
        if active:
            if active != self._active:
                self._log(f"upgrade waits for {active} active run(s) to end")
            self._active = active
            return
        self._log(f"no run active: upgrading to {self._ref}")
        checkout = pin(self._settings, self._ref, self._uv, self._repo)
        self.pinned, self.pending, self._active = checkout.name, None, None
        self._restore_at = self._clock() if self._paused.exists() else None
        self._log(f"pinned {short(checkout.name)}: fact dev starts it")

    def _restore_on(self, now: float) -> None:
        """Turn the scheduler back on once the restarted API answers; it is not up yet on the first tries."""
        self._restore_at = now + RESTORE_INTERVAL_S
        try:
            call_scheduler("POST", "/on", retry=False)
        except CHECK_ERRORS:  # not up yet
            return
        self._paused.unlink(missing_ok=True)
        self._restore_at = None
        self._log("scheduler back on, as it was before the upgrade")
