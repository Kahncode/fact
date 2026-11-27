"""
`fact shutdown`: stop the factory safely, such as before a reboot.

The command turns the scheduler off and sets its `shutdown` flag (`POST /scheduler/shutdown`, dev only), then returns.
`fact dev`, in every mode, polls `GET /scheduler` and `GET /scheduler/active` every `POLL_INTERVAL_S` with a
`ShutdownWatch`: once `shutdown` is set and no run is active (human waits included), it stops Temporal, the API and
the worker and exits 0. `fact scheduler on` clears the flag, which cancels the shutdown. The flag lives in the API
process only: an API restart drops it. There is no deadline and no forced stop.
"""

import time
from collections.abc import Callable
from typing import Any

import httpx
import typer
from fact.cli import projects
from fact.cli.runs import API, api_error, call_api
from fact.cli.supervisor import StopRequestedError

POLL_INTERVAL_S = 5.0
SCHEDULER = f"{API}/scheduler"
# what a poll can raise: the API down (starting, restarting) or answering oddly
POLL_ERRORS = (httpx.HTTPError, KeyError, TypeError, ValueError)


def call_scheduler(method: str, path: str) -> dict[str, Any]:
    """Call `/api/v1/scheduler<path>` for `fact dev`; errors are raised for the caller to retry at its next poll."""
    with projects.make_client() as client:
        response = client.request(method, f"{SCHEDULER}{path}")
    response.raise_for_status()
    return response.json()


class ShutdownWatch:
    """Polled by `fact dev`; raises `StopRequestedError` once `fact shutdown` was asked and no run is active."""

    def __init__(self, log: Callable[[str], None], clock: Callable[[], float] = time.monotonic) -> None:
        self._log, self._clock = log, clock
        self.shutdown = False  # the API's flag at the last poll that answered
        self._active: int | None = None
        self._next = clock() + POLL_INTERVAL_S

    def __call__(self) -> None:
        now = self._clock()
        if now < self._next:
            return
        self._next = now + POLL_INTERVAL_S
        try:
            self._poll()
        except POLL_ERRORS:  # the API is not answering; keep the last state and poll again
            return

    def _poll(self) -> None:
        self.shutdown = bool(call_scheduler("GET", "").get("shutdown"))
        if not self.shutdown:
            if self._active is not None:
                self._log("shutdown cancelled")
            self._active = None
            return
        active = int(call_scheduler("GET", "/active")["active"])
        if not active:
            msg = "shutdown: no run active, stopping"
            raise StopRequestedError(msg)
        if active != self._active:
            self._log(f"shutdown waits for {active} active run(s) to end")
        self._active = active


def shutdown() -> None:
    """Stop starting runs and let `fact dev` exit once the active ones end (`fact scheduler on` cancels)."""
    try:
        call_api("POST", f"{SCHEDULER}/shutdown")
        active = int(call_api("GET", f"{SCHEDULER}/active")["active"])
    except httpx.HTTPStatusError as e:
        raise api_error(e) from e
    typer.echo("Shutdown: the scheduler is off, no new run starts.")
    if active:
        typer.echo(f"{active} run(s) active: `fact dev` exits once they end.")
    else:
        typer.echo("No run active: `fact dev` exits within a few seconds.")
    typer.echo("`fact scheduler on` cancels the shutdown.")
