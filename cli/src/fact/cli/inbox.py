"""`fact inbox`: the notifications waiting for you, until the UI's Notifications page exists."""

from typing import Annotated

import httpx
import typer
from fact.cli.projects import format_table
from fact.cli.runs import API, api_error, call_api

app = typer.Typer(help="Notifications: runs waiting for you, PRs ready, runs that failed.")


@app.callback(invoke_without_command=True)
def inbox(
    ctx: typer.Context,
    *,
    show_all: Annotated[bool, typer.Option("--all", help="Also show notifications already read.")] = False,
) -> None:
    """List unread notifications, newest first."""
    if ctx.invoked_subcommand is not None:
        return
    params = {} if show_all else {"unread": "true"}
    try:
        notifications = call_api("GET", f"{API}/notifications", params=params)
    except httpx.HTTPStatusError as e:
        raise api_error(e) from e
    if not notifications:
        typer.echo("No notifications." if show_all else "No unread notifications.")
        return
    rows = [
        (
            str(n["id"]),
            n["kind"],
            n["title"],
            n["created_at"][:19],
            "read" if n["read_at"] else "unread",
            n["link"] or "-",
        )
        for n in notifications
    ]
    typer.echo(format_table(("ID", "KIND", "TITLE", "CREATED", "STATE", "LINK"), rows))


@app.command("read")
def read(
    notification_id: Annotated[int, typer.Argument(metavar="ID", help="Notification id from `fact inbox`.")],
) -> None:
    """Mark a notification read."""
    try:
        call_api("POST", f"{API}/notifications/{notification_id}/read")
    except httpx.HTTPStatusError as e:
        raise api_error(e) from e
    typer.echo(f"Marked notification {notification_id} read.")
