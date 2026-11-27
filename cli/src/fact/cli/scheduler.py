"""
`fact scheduler on|off|status`: switch the API's tick loop on and off (dev only), and describe the scheduler.

Outside dev the API has no loop and no switch (`on`/`off` answer 404): an outside cron calls `POST /scheduler/tick`.

`status` also previews the next tick (`GET /scheduler/preview`): the ready tasks in start order, why each one waits,
and the usage gate's last answer. The preview never calls the usage endpoint itself.
"""

from typing import Any

import httpx
import typer
from fact.cli.runs import API, api_error, call_api

app = typer.Typer(help="The scheduler: starts ready tasks by itself while it is on.", no_args_is_help=True)


def _show(state: dict[str, Any]) -> None:
    if state["in_process"]:
        shutting = " (shutting down: `fact dev` exits once no run is active)" if state.get("shutdown") else ""
        typer.echo(f"Scheduler: {'on' if state['on'] else 'off'} (ticks in the API){shutting}")
        typer.echo(f"Every:     {state['interval_seconds']:g} s")
    else:
        typer.echo("Scheduler: ticks only on POST /api/v1/scheduler/tick (an outside cron or webhook)")
    last = state.get("last_tick")
    if state.get("last_tick_at") and last:
        what = f"{len(last['started'])} started, {len(last['failed'])} failed"
        if last.get("usage_full"):
            what = f"usage full: {last['usage_full']}"
        typer.echo(f"Last tick: {state['last_tick_at'][:19]} ({what})")


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
        typer.echo(f"  {task['task_key']:<28} {task['project']:<12} {_priority(task):<16} {why}")


def _priority(task: dict[str, Any]) -> str:
    """Show the task's priority, and its effective priority beside it when a task waiting on it raised it."""
    own, effective = task.get("priority", "medium"), task.get("effective_priority", "medium")
    return own if effective == own else f"{own} (eff. {effective})"


@app.command("on")
def on() -> None:
    """Turn the API's tick loop on (dev only): ready tasks start by themselves."""
    _show(_call("POST", "/on"))


@app.command("off")
def off() -> None:
    """Turn the API's tick loop off (dev only, until the API restarts): no new run starts by itself."""
    _show(_call("POST", "/off"))


@app.command("status")
def status() -> None:
    """Show whether the scheduler is on, its last tick, what it would start next and the Claude usage it last saw."""
    _show(_call("GET", ""))
    _show_preview(_call("GET", "/preview"))
