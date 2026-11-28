"""`fact shutdown` against real processes recorded in a run directory, with no `fact dev` running."""

import sys

from fact.cli import processes
from fact.cli.dev import run_dir
from fact.cli.main import app
from fact.cli.processes import ChildSpec
from fact.core.settings import Settings
from typer.testing import CliRunner

# Exits once its stop file appears, as the API and the worker do.
GRACEFUL = [
    sys.executable,
    "-c",
    (
        "import os, pathlib, time\nstop = pathlib.Path(os.environ['FACT_STOP_FILE'])\n"
        "while not stop.exists(): time.sleep(0.05)\n"
    ),
]
SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


def test_shutdown_stops_the_workers_then_the_api_then_temporal(settings: Settings) -> None:
    run = run_dir(settings)
    children = [
        processes.start(ChildSpec("temporal", SLEEPER, graceful=False), run),
        processes.start(ChildSpec("api", GRACEFUL), run),
        processes.start(ChildSpec("worker", GRACEFUL), run),
    ]
    try:
        result = CliRunner().invoke(app, ["shutdown"])

        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        assert lines[:3] == ["stopping worker", "stopping api", "stopping temporal"]
        assert "carry on at the next `fact dev`" in result.output
        assert all(c.exited() for c in children)
        assert run.records() == {}
        assert children[1].exit_code() == 0  # stopped by its stop file, not killed
    finally:
        for child in children:
            if child.proc is not None and not child.exited():
                processes.kill_tree(child.proc)


def test_shutdown_with_nothing_running(settings: Settings) -> None:  # noqa: ARG001 - an empty data_dir
    result = CliRunner().invoke(app, ["shutdown"])

    assert result.exit_code == 0, result.output
    assert "nothing to stop" in result.output
