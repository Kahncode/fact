"""`fact projects ...`: projects the API knows about."""

from collections.abc import Sequence

import httpx
import typer
from fact.core.http_retry import CLI_POLICY, RetryTransport
from fact.core.settings import get_settings

HTTP_TIMEOUT_S = 10.0

app = typer.Typer(no_args_is_help=True, help="Projects registered in fact.yaml.")


def make_client() -> httpx.Client:
    """
    HTTP client for the API, retrying a few seconds while it restarts (`CLI_POLICY`).

    Tests replace this to inject an `httpx.MockTransport`.
    """
    transport = RetryTransport(CLI_POLICY)
    return httpx.Client(base_url=get_settings().api_url, timeout=HTTP_TIMEOUT_S, transport=transport)


def format_table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    lines = ["  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip() for row in [header, *rows]]
    return "\n".join(lines)


@app.command("list")
def list_projects() -> None:
    """List projects with their home and repositories."""
    with make_client() as client:
        try:
            response = client.get("/api/v1/projects")
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            typer.echo(f"error: API answered {e.response.status_code}: {e.response.text}", err=True)
            raise typer.Exit(1) from e
        except httpx.HTTPError as e:
            typer.echo(f"error: cannot reach the API at {client.base_url} ({e}). Is `fact dev` running?", err=True)
            raise typer.Exit(1) from e
    try:
        rows = [
            (p["name"], p["home_repo"], ", ".join(r["full_name"] for r in p["repositories"]) or "-")
            for p in response.json()
        ]
    except (ValueError, KeyError, TypeError) as e:
        typer.echo(f"error: unexpected API response: {response.text[:200]}", err=True)
        raise typer.Exit(1) from e
    if not rows:
        typer.echo("No projects.")
        return
    typer.echo(format_table(("NAME", "HOME", "REPOSITORIES"), rows))
