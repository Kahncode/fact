"""
`fact scheduler on|off|status`: unpause, pause and describe the `fact-scheduler` Temporal Schedule.

`status` also previews the next tick (`GET /scheduler/preview`): the ready tasks in start order, why each one waits,
and the usage gate's last answer. The preview never calls the usage endpoint itself.
"""

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


def _call(method: str, path: str) -> dict[str, Any]:
    try:
        return call_api(method, f"{API}/scheduler{path}")
    except httpx.HTTPStatusError as e:
        raise api_error(e) from e


def _show_usage(usage: dict[str, Any] | None) -> None:
    if usage is None:
        typer.echo("Usage:     no answer yet (the gate is checked when a task would start)")
        return
    state = usage["state"]
    detail = usage.get("error") or usage.get("reason") or ""
    typer.echo(f"Usage:     {state}" + (f": {detail}" if detail and state != "full" else ""))
    for w in usage.get("windows", []):
        mark = "  FULL" if w["full"] else ""
        typer.echo(
            f"  {w['name']:<17}{w['utilization']:>4.0%} used, reserve {w['reserve']:.0%}, "
            f"resets {w['resets_at'][:16].replace('T', ' ')} UTC{mark}"
        )
    if state == "full" and usage.get("next_call_at"):
        typer.echo(f"  next check {usage['next_call_at'][:16].replace('T', ' ')} UTC")


def _show_preview(preview: dict[str, Any]) -> None:
    typer.echo(f"Runs:      {preview['active']} active of max {preview['max_runs']}")
    _show_usage(preview.get("usage"))
    tasks = preview.get("tasks", [])
    if not tasks:
        typer.echo("Ready:     nothing to start")
        return
    typer.echo("Ready, in start order:")
    for planned in tasks:
        task, hold = planned["task"], planned.get("hold")
        why = f"waits ({hold}): {planned['detail']}" if hold else "starts next tick"
        typer.echo(f"  {task['task_key']:<28} {task['project']:<12} {why}")


@app.command("on")
def on() -> None:
    """Unpause the scheduler: ready tasks start by themselves."""
    _show(_call("POST", "/on"))


@app.command("off")
def off() -> None:
    """Pause the scheduler: no new run starts by itself; runs in progress go on."""
    _show(_call("POST", "/off"))


@app.command("status")
def status() -> None:
    """Show whether the scheduler is on, its next tick, what it would start next and the Claude usage it last saw."""
    _show(_call("GET", ""))
    _show_preview(_call("GET", "/preview"))
