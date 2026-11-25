"""
Print the next runnable task file (quick and dirty, stdlib only).

A task is done when its file is gone (deleted on completion) or its status is `done`.
Candidates are tasks that are not done and whose depends_on are all done, in milestone/number order,
except that reopened tasks (with a `resume` field) come first.

    uv run python .claude/skills/next-task/next_task.py          # the next task path
    uv run python .claude/skills/next-task/next_task.py --all    # every runnable task
"""

import re
import sys
from pathlib import Path

TASKS = Path(__file__).resolve().parents[3] / "tasks"


def front_matter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    block = text.split("---", 2)[1]
    fields = {}
    for line in block.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.split("#")[0].strip()
    return fields


def main() -> int:
    tasks = {}
    for path in sorted(TASKS.glob("M*/*.md")):
        meta = front_matter(path)
        tasks[meta["id"].strip("\"'")] = (path, meta)
    runnable = []
    for task_id, (path, meta) in tasks.items():
        if meta.get("status") == "done":
            continue
        deps = re.findall(r"M\d+-\d+", meta.get("depends_on", ""))
        if all(d not in tasks or tasks[d][1].get("status") == "done" for d in deps):
            runnable.append((task_id, path))
    # Reopened tasks (resume: <commit>) first: a run hit the usage limit, so their commit is unverified.
    runnable.sort(key=lambda item: not tasks[item[0]][1].get("resume", "").strip("\"'"))
    if not runnable:
        print("No runnable task.", file=sys.stderr)
        return 1
    for task_id, path in runnable if "--all" in sys.argv else runnable[:1]:
        print(f"{task_id}\t{path.relative_to(TASKS.parent).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
