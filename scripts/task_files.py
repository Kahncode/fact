"""Task files: parse the front matter and find the tasks that can run now (used by task_loop.py)."""

# ruff: noqa: INP001
import re
from dataclasses import dataclass
from pathlib import Path

TASKS = Path(__file__).resolve().parents[1] / "tasks"


@dataclass
class Task:
    id: str
    path: Path
    complexity: str
    status: str
    depends_on: list[str]
    human_actions: list[str]  # things only a person can do; the task is not runnable while any are listed
    resume: str = ""  # commit to verify: the task was reopened after a run hit the usage limit


def parse_task(path: Path) -> Task:
    block = path.read_text(encoding="utf-8").split("---", 2)[1]
    fields: dict[str, str] = {}
    for line in block.splitlines():
        key, sep, value = line.partition(":")
        if sep and not line.startswith((" ", "\t")):
            fields[key.strip()] = value.split("#")[0].strip()
    return Task(
        id=fields["id"].strip("\"'"),
        path=path,
        complexity=fields.get("complexity", "standard"),
        status=fields.get("status", "planned"),
        depends_on=re.findall(r"M\d+-\d+", fields.get("depends_on", "")),
        human_actions=parse_list(block, "human_actions"),
        resume=fields.get("resume", "").strip("\"'"),
    )


def parse_list(block: str, key: str) -> list[str]:
    """Read a front-matter list, inline (`key: [a, "b"]`) or as `  - item` lines below the key."""
    lines = block.split(f"{key}:", 1)[-1].splitlines() if f"{key}:" in block else []
    if not lines:
        return []
    inline = lines[0].split("#")[0].strip().strip("[]")
    items = [i for i in inline.split(",") if i.strip()] if inline else []
    for line in lines[1:]:
        if not line.lstrip().startswith("- "):
            break
        items.append(line.lstrip()[2:])
    return [i.strip().strip("\"'") for i in items]


def load_tasks(tasks_dir: Path = TASKS) -> dict[str, Task]:
    return {t.id: t for t in (parse_task(p) for p in sorted(tasks_dir.glob("M*/*.md")))}


def runnable_tasks(tasks_dir: Path = TASKS) -> list[Task]:
    """
    Not done, every dependency done (file gone or status done), no human action pending.

    Reopened tasks (with `resume`) come first: their commit is unverified and later work may build on it.
    """
    tasks = load_tasks(tasks_dir)
    runnable = [
        t
        for t in tasks.values()
        if t.status != "done"
        and not t.human_actions
        and all(d not in tasks or tasks[d].status == "done" for d in t.depends_on)
    ]
    return sorted(runnable, key=lambda t: not t.resume)  # stable: path order otherwise
