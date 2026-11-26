"""`fact scheduler on|off|status`: unpause, pause and describe the `fact-scheduler` Temporal Schedule."""

from typing import Any

import httpx
import typer
from fact.cli.runs import API, api_error, call_api

app = typer.Typer(help="The scheduler: starts ready tasks by itself while it is on.", no_args_is_help=True)


def _show(state: dict[str, Any]) -> None:
    every = state.get("interval_seconds")
    typer.echo(f"Scheduler {state['schedule_id']}: {'off (paused)' if state['paused'] else 'on'}")
    if every is not None:
        typer.echo(f"Every:     {every:g} s")
    if state.get("note"):
        typer.echo(f"Note:      {state['note']}")
    if state.get("next_ticks") and not state["paused"]:
        typer.echo(f"Next tick: {state['next_ticks'][0][:19]}")
    if state.get("recent_ticks"):
        typer.echo(f"Last tick: {state['recent_ticks'][-1][:19]}")


def _call(method: str, path: str) -> None:
    try:
        state = call_api(method, f"{API}/scheduler{path}")
    except httpx.HTTPStatusError as e:
        raise api_error(e) from e
    _show(state)


@app.command("on")
def on() -> None:
    """Unpause the scheduler: ready tasks start by themselves."""
    _call("POST", "/on")


@app.command("off")
def off() -> None:
    """Pause the scheduler: no new run starts by itself; runs in progress go on."""
    _call("POST", "/off")


@app.command("status")
def status() -> None:
    """Show whether the scheduler is on, its interval and its next tick."""
    _call("GET", "")
