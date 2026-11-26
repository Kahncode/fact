"""`fact run`, `fact runs`, `fact status`, `fact gate` and `fact steer`: start runs, follow and answer them."""

from datetime import datetime
from typing import Annotated, Any
from urllib.parse import quote

import httpx
import typer
from fact.cli import projects
from fact.cli.projects import format_table
from fact.core.enums import RunStatus
from fact.core.gates import GateDecision
from fact.core.settings import get_settings

API = "/api/v1"
WORKFLOW_ID_PREFIX = "run-"
SECONDS_PER_MINUTE = 60
HTTP_CONFLICT = 409


def _fail(message: str) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(1)


def call_api(method: str, path: str, **kwargs: Any) -> Any:  # noqa: ANN401
    """Call the API and return its JSON; HTTP errors come back as `httpx.HTTPStatusError` for the caller to read."""
    with projects.make_client() as client:
        try:
            response = client.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            msg = f"cannot reach the API at {client.base_url} ({e}). Is `fact dev` running?"
            raise _fail(msg) from e
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as e:
        msg = f"unexpected API response: {response.text[:200]}"
        raise _fail(msg) from e


def _detail(e: httpx.HTTPStatusError) -> Any:  # noqa: ANN401
    try:
        return e.response.json().get("detail", e.response.text)
    except ValueError, AttributeError:
        return e.response.text


def api_error(e: httpx.HTTPStatusError) -> typer.Exit:
    return _fail(f"API answered {e.response.status_code}: {_detail(e)}")


def run(
    task_key: Annotated[str, typer.Argument(help="Task key, such as task:M1-02.")],
    *,
    force_deps: Annotated[bool, typer.Option("--force-deps", help="Start even if dependencies are not done.")] = False,
    project: Annotated[
        str | None, typer.Option(help="Project holding the task; found from the key if omitted.")
    ] = None,
) -> None:
    """Start the project's workflow for a task."""
    body = {"task_key": task_key, "project": project, "override_deps": force_deps}
    try:
        started = call_api("POST", f"{API}/runs/start", json=body)
    except httpx.HTTPStatusError as e:
        detail = _detail(e)
        if e.response.status_code == HTTP_CONFLICT and isinstance(detail, dict) and "missing_deps" in detail:
            missing = ", ".join(detail["missing_deps"])
            msg = f"{task_key} has unfinished dependencies: {missing}. Use --force-deps to start anyway."
            raise _fail(msg) from e
        raise api_error(e) from e
    typer.echo(f"Started {started['workflow_id']} ({started['workflow_type']}, project {started['project']})")
    typer.echo(f"Run:      {started['run_id']}")
    typer.echo(f"Temporal: {started['temporal_url']}")


def _cost(value: object) -> str:
    return f"${float(str(value)):.2f}"


def list_runs(
    *,
    project: Annotated[str | None, typer.Option(help="Only this project's runs.")] = None,
    status: Annotated[str | None, typer.Option(help="Only runs with this status, such as running.")] = None,
) -> None:
    """List runs, newest first."""
    params = {k: v for k, v in {"project": project, "status": status}.items() if v is not None}
    try:
        runs = call_api("GET", f"{API}/runs", params=params)
    except httpx.HTTPStatusError as e:
        raise api_error(e) from e
    if not runs:
        typer.echo("No runs.")
        return
    rows = [
        (r["workflow_id"], r["status"], r["outcome"] or "-", _cost(r["cost_usd"]), r["created_at"][:19], r["id"])
        for r in runs
    ]
    typer.echo(format_table(("WORKFLOW", "STATUS", "OUTCOME", "COST", "CREATED", "RUN"), rows))


def _duration(start: str | None, end: str | None) -> str:
    if not start or not end:
        return "-"
    seconds = int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
    minutes, secs = divmod(max(seconds, 0), SECONDS_PER_MINUTE)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def _find_run(ref: str) -> dict[str, Any]:
    if ref.startswith(WORKFLOW_ID_PREFIX):
        matches = call_api("GET", f"{API}/runs", params={"workflow_id": ref})
        if not matches:
            msg = f"no run with workflow id {ref}"
            raise _fail(msg)
        ref = matches[0]["id"]
    return call_api("GET", f"{API}/runs/{quote(ref, safe='')}")


def _links(step: dict[str, Any], base: str) -> str:
    refs = [(name, step[key]) for name, key in (("log", "log_ref"), ("transcript", "transcript_ref")) if step[key]]
    return " ".join(f"{name}: {base}{API}/artifacts/{ref}" for name, ref in refs) or "-"


def status(run_ref: Annotated[str, typer.Argument(metavar="RUN", help="Run id or workflow id (run-...).")]) -> None:
    """Show a run with its steps, durations, outcome, cost and log links."""
    try:
        run_detail = _find_run(run_ref)
    except httpx.HTTPStatusError as e:
        raise api_error(e) from e
    r = run_detail
    typer.echo(f"{r['workflow_id']}  {r['task_key']}  ({r['workflow_type']})")
    typer.echo(f"Run:      {r['id']}")
    typer.echo(f"Status:   {r['status']}  outcome: {r['outcome'] or '-'}")
    typer.echo(f"Duration: {_duration(r['started_at'], r['ended_at'])}  machine: {r['machine_seconds']:.0f}s")
    typer.echo(f"Cost:     {_cost(r['cost_usd'])}  tokens in/out: {r['tokens_in']}/{r['tokens_out']}")
    if r["status"] != RunStatus.FINISHED:
        typer.echo(f"Gate:     {_gate_line(r['id'])}")
    if not r["steps"]:
        typer.echo("\nNo steps yet.")
        return
    base = get_settings().api_url.rstrip("/")
    rows = [
        (
            s["stage_id"],
            str(s["attempt"]),
            s["kind"],
            s["status"],
            s["outcome"] or "-",
            _duration(s["started_at"], s["ended_at"]),
            _cost(s["cost_usd"]),
            _links(s, base),
        )
        for s in r["steps"]
    ]
    typer.echo("")
    typer.echo(format_table(("STAGE", "#", "KIND", "STATUS", "OUTCOME", "TIME", "COST", "LINKS"), rows))


def _live(run_id: str) -> dict[str, Any]:
    return call_api("GET", f"{API}/runs/{quote(run_id, safe='')}/status")


def _gate_line(run_id: str) -> str:
    """Describe the open gate from the workflow's status query; a failed query is shown, not fatal."""
    try:
        gate = _live(run_id)["gate"]
    except httpx.HTTPStatusError as e:
        return f"unknown ({_detail(e)})"
    if gate is None:
        return "none open"
    return (
        f"{gate['gate_id']} ({gate['kind']}, {gate['stage_id']} #{gate['attempt']}) open since {gate['opened_at'][:19]}"
    )


def gate(
    run_ref: Annotated[str, typer.Argument(metavar="RUN", help="Run id or workflow id (run-...).")],
    decision: Annotated[GateDecision, typer.Argument(help="approve or reject.")],
    *,
    note: Annotated[str, typer.Option(help="Note for the run, such as why you reject.")] = "",
    gate_id: Annotated[
        str | None, typer.Option("--gate", help="Gate id to answer; the run's open gate if omitted.")
    ] = None,
) -> None:
    """Answer the gate a run is waiting at."""
    try:
        run_id = _find_run(run_ref)["id"]
        if gate_id is None:
            open_gate = _live(run_id)["gate"]
            if open_gate is None:
                msg = f"{run_ref} has no open gate"
                raise _fail(msg)
            gate_id = str(open_gate["gate_id"])
        path = f"{API}/runs/{quote(run_id, safe='')}/gates/{quote(gate_id, safe='')}"
        call_api("POST", path, json={"decision": decision.value, "note": note})
    except httpx.HTTPStatusError as e:
        raise api_error(e) from e
    typer.echo(f"Answered gate {gate_id} of {run_ref}: {decision.value}")


def steer(
    run_ref: Annotated[str, typer.Argument(metavar="RUN", help="Run id or workflow id (run-...).")],
    text: Annotated[str, typer.Argument(help="Instructions for the run's next agent session.")],
) -> None:
    """Add instructions to a run's next agent session; a running session is not interrupted."""
    try:
        run_id = _find_run(run_ref)["id"]
        call_api("POST", f"{API}/runs/{quote(run_id, safe='')}/instructions", json={"text": text})
    except httpx.HTTPStatusError as e:
        raise api_error(e) from e
    typer.echo(f"Queued instructions for {run_ref}'s next agent session.")
