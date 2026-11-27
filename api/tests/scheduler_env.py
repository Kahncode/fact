"""Shared by the scheduler tests: a factory with project `acme` whose origin/main holds more ready tasks, and fakes."""

from datetime import datetime
from pathlib import Path

from fact.core.claude_usage import ClaudeUsage, UsageWindow
from httpx import AsyncClient
from task_repo import PROJECT, PROJECT_YAML, git, task_file, write


def write_config(tmp_path: Path, checkout: Path, *, project_cap: int = 3) -> Path:
    """
    `acme` on origin/main: M1-02 (depth 1), M1-03 (waits on M1-02), M2-01 and M2-02 (depth 0); `broken` is skipped.

    `remote` has no checkout: its task source cannot be read without GitHub, so it offers nothing. `acme`'s cap is
    its runner pool's `max_concurrent_runs` in `.fact/project.yaml` (read from the working tree, like its config).
    """
    pool = PROJECT_YAML.replace("max_concurrent_runs: 1", f"max_concurrent_runs: {project_cap}")
    write(checkout, {".fact/project.yaml": pool})
    write(checkout, {"tasks/M2/01-next.md": task_file("M2-01", []), "tasks/M2/02-later.md": task_file("M2-02", [])})
    git(checkout, "add", "tasks/M2")  # M1-04 stays in the working tree only
    git(checkout, "commit", "-m", "more tasks")
    git(checkout, "push", "origin", "main")
    broken = tmp_path / "broken"  # a valid config, but not a git checkout: skipped, not fatal
    write(broken, {".fact/project.yaml": PROJECT_YAML})
    config = tmp_path / "fact.yaml"
    config.write_text(
        f"data_dir: {(tmp_path / 'data').as_posix()}\n"
        f"projects:\n  - name: {PROJECT}\n    home: acme/home\n    path: {checkout.as_posix()}\n"
        f"  - name: remote\n    home: acme/remote\n  - name: broken\n    home: acme/b\n    path: {broken.as_posix()}\n",
        encoding="utf-8",
    )
    return config


async def add_run(client: AsyncClient, n: int, task_key: str, trigger: str = "manual", **update: str) -> None:
    body = {"workflow_id": f"run-{n}", "project": PROJECT, "task_key": task_key, "workflow_type": "standard"}
    body["trigger"] = trigger
    run = (await client.post("/api/v1/runs", json=body)).json()
    if update:
        await client.patch(f"/api/v1/runs/{run['id']}", json=update)


class FakeTemporal:
    """`app.state.temporal` whose client records each started workflow id; `forbid` fails any use of Temporal."""

    address = "test"

    def __init__(self, *, forbid: bool = False) -> None:
        self.forbid = forbid
        self.started: list[str] = []

    async def client(self) -> FakeTemporal:
        assert not self.forbid, "the tick touched Temporal"
        return self

    async def start_workflow(self, _type: str, _arg: object, *, id: str, task_queue: str) -> None:  # noqa: A002, ARG002
        self.started.append(id)


class FakeUsage:
    """The usage endpoint behind `UsageGate`: answers `answer` and counts the calls."""

    def __init__(self, answer: ClaudeUsage | None = None) -> None:
        self.answer = answer or ClaudeUsage()
        self.calls = 0

    def __call__(self) -> ClaudeUsage:
        self.calls += 1
        return self.answer


def five_hour(utilization: float, resets_at: datetime) -> ClaudeUsage:
    return ClaudeUsage(windows=(UsageWindow(name="five_hour", utilization=utilization, resets_at=resets_at),))
