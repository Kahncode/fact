"""The `fact` command."""

from importlib.metadata import version
from typing import Annotated

import typer
from fact.cli import dev, github, inbox, projects, runs, scheduler, use

app = typer.Typer(name="fact", no_args_is_help=True, help="fact: turn tasks into reviewed GitHub PRs.")
app.command("dev")(dev.dev)
app.command("use")(use.use)
app.command("run")(runs.run)
app.command("runs")(runs.list_runs)
app.command("status")(runs.status)
app.command("gate")(runs.gate)
app.command("steer")(runs.steer)
app.command("drop")(runs.drop)
app.add_typer(inbox.app, name="inbox")
app.add_typer(projects.app, name="projects")
app.add_typer(github.app, name="github")
app.add_typer(scheduler.app, name="scheduler")


def _print_version(value: bool) -> None:  # noqa: FBT001
    if value:
        typer.echo(f"fact {version('fact-cli')}")
        raise typer.Exit


@app.callback()
def main(
    *,
    _version: Annotated[
        bool,
        typer.Option("--version", callback=_print_version, is_eager=True, help="Show the version and exit."),
    ] = False,
) -> None:
    """fact: turn tasks into reviewed GitHub PRs."""


if __name__ == "__main__":
    app()
