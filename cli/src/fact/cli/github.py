"""`fact github ...`: check fact's GitHub App access and set up a repository's labels."""

import asyncio
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

import httpx
import typer
from fact.core.github import GitHubApp, GitHubError, LabelAction, sync_labels
from fact.core.logging import configure_logging
from fact.core.settings import get_settings

app = typer.Typer(no_args_is_help=True, help="fact's GitHub App.")
labels_app = typer.Typer(no_args_is_help=True, help="The labels fact uses on a task repository.")
app.add_typer(labels_app, name="labels")

RepoArg = Annotated[str, typer.Argument(help="Repository as owner/repo.")]


def make_app() -> GitHubApp:
    """Build the App from settings; tests replace this to inject an `httpx.MockTransport`."""
    return GitHubApp.from_settings()


def _run(work: Callable[[], Coroutine[Any, Any, list[str]]]) -> None:
    """Run `work` and print its lines; turn GitHub, config and network errors into a one-line error and exit 1."""
    configure_logging(get_settings().env, "WARNING")  # keep the report clean; errors still show
    try:
        lines = asyncio.run(work())
    except (GitHubError, ValueError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from e
    except (KeyError, TypeError) as e:
        typer.echo(f"error: unexpected GitHub response (missing {e})", err=True)
        raise typer.Exit(1) from e
    except httpx.HTTPError as e:
        typer.echo(f"error: cannot reach GitHub ({type(e).__name__}: {e})", err=True)
        raise typer.Exit(1) from e
    for line in lines:
        typer.echo(line)


async def _check(full_name: str) -> list[str]:
    gh = make_app()
    info = await gh.app_info()
    token = await gh.installation_token(full_name)
    return [
        f"app:          {info.slug} (id {info.id})",
        f"installation: {token.installation.id} (account {token.installation.account})",
        f"token:        expires {token.expires_at.isoformat()}",
    ]


async def _sync_labels(full_name: str) -> list[str]:
    gh = make_app()
    token = await gh.installation_token(full_name)
    async with gh.api_client(token.token) as client:
        changes = await sync_labels(client, full_name)
    lines = [f"{c.action:<9}  {c.name}" for c in changes]
    changed = sum(c.action is not LabelAction.UNCHANGED for c in changes)
    lines.append(f"{changed} of {len(changes)} labels changed on {full_name}")
    return lines


@app.command("check")
def check(repo: RepoArg) -> None:
    """Mint an installation token for a repository and print the app, installation and expiry (never the token)."""
    _run(lambda: _check(repo))


@labels_app.command("sync")
def labels_sync(repo: RepoArg) -> None:
    """Create fact's labels on a repository and fix colour and description of existing ones. Never deletes labels."""
    _run(lambda: _sync_labels(repo))
