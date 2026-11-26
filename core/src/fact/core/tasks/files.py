"""File task source: reads the markdown task files in `tasks/` (format in `tasks/README.md`)."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from fact.core.types import Task
from pydantic import ValidationError

KEY_PREFIX = "task:"
STATUSES = ("planned", "ready", "done")
_ID_RE = re.compile(r"^M(\d+)-(\d+)$")
_FILE_RE = re.compile(r"^(\d+)-.+\.md$")
_CRITERION_RE = re.compile(r"^\s*-\s*\[[ xX]\]\s*(.+?)\s*$")


def branch_name(key: str) -> str:
    """`task:M1-02` becomes `fact/task-m1-02`."""
    return f"fact/task-{key.removeprefix(KEY_PREFIX).lower()}"


@dataclass(frozen=True)
class _Entry:
    task: Task
    status: str
    moved: bool = False


def _split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        msg = "missing YAML front matter"
        raise ValueError(msg)
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            meta = yaml.safe_load("".join(lines[1:i]))
            if not isinstance(meta, dict):
                msg = "front matter is not a mapping"
                raise ValueError(msg)
            return meta, "".join(lines[i + 1 :]).strip("\r\n")
    msg = "unterminated YAML front matter"
    raise ValueError(msg)


def _criteria(body: str) -> list[str]:
    found: list[str] = []
    in_section = False
    for line in body.splitlines():
        if line.startswith("## "):
            in_section = line[3:].strip().lower() == "acceptance criteria"
        elif in_section and (m := _CRITERION_RE.match(line)):
            found.append(m.group(1))
    return found


def _parse(path: Path, rel: str) -> _Entry:
    meta, body = _split_front_matter(path.read_text(encoding="utf-8-sig"))
    if "title" not in meta:
        msg = "front matter has no title"
        raise ValueError(msg)
    task_id = str(meta.get("id", ""))
    task = Task(
        key=KEY_PREFIX + task_id,
        id=task_id,
        title=str(meta["title"]),
        body=body.replace("\r\n", "\n"),
        acceptance_criteria=_criteria(body),
        type=meta.get("type", "feature"),
        complexity=meta.get("complexity", "standard"),
        depends_on=[str(d) for d in meta.get("depends_on") or []],
        path=rel,
        require_plan_approval=meta.get("require_plan_approval"),
    )
    return _Entry(task, str(meta.get("status", "")), moved=bool(meta.get("moved_to")))


def _milestone_num(task_id: str) -> tuple[int, int] | None:
    m = _ID_RE.match(task_id)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _is_finished(dep: str, entries: dict[str, _Entry]) -> bool:
    """
    Tell whether a missing id is a finished task, whose file was deleted.

    It counts as done if it could have existed: its number is below the highest remaining in its milestone, or its milestone is below the
    highest milestone still present. An id past both (a typo) is unknown.
    """
    wanted = _milestone_num(dep)
    if wanted is None:
        return False
    present = [p for e in entries.values() if (p := _milestone_num(e.task.id))]
    same = [num for mil, num in present if mil == wanted[0]]
    if same:
        return wanted[1] < max(same)
    return bool(present) and wanted[0] < max(mil for mil, _ in present)


def _cycle_errors(entries: dict[str, _Entry]) -> list[str]:
    errors: list[str] = []
    state: dict[str, int] = {}  # 1 = on the current path, 2 = finished

    def visit(key: str, path: list[str]) -> None:
        state[key] = 1
        for dep in entries[key].task.depends_on:
            dk = KEY_PREFIX + dep
            if dk not in entries:
                continue
            if state.get(dk) == 1:
                cycle = [*path[path.index(dk) :], dk]
                errors.append("dependency cycle: " + " -> ".join(k.removeprefix(KEY_PREFIX) for k in cycle))
            elif dk not in state:
                visit(dk, [*path, dk])
        state[key] = 2

    for key in entries:
        if key not in state:
            visit(key, [key])
    return errors


class FileTaskSource:
    """Task files live at `<root>/<folder>/<milestone>/<NN>-<slug>.md`. Every call re-reads the files."""

    def __init__(self, root: Path, folder: str = "tasks") -> None:
        self.root = root
        self.folder = folder

    def _scan(self) -> tuple[list[_Entry], list[str]]:
        entries: list[_Entry] = []
        errors: list[str] = []
        for path in sorted((self.root / self.folder).glob("*/*.md")):
            m = _FILE_RE.match(path.name)
            if m is None:
                continue
            rel = path.relative_to(self.root).as_posix()
            try:
                entry = _parse(path, rel)
            except (ValueError, ValidationError, yaml.YAMLError) as e:
                errors.append(f"{rel}: {e}".replace("\n", " "))
                continue
            if entry.moved:
                continue
            expected = f"{path.parent.name}-{m.group(1)}"
            if entry.task.id != expected:
                errors.append(f"{rel}: id {entry.task.id!r} does not match its file name (expected {expected})")
            entries.append(entry)
        return entries, errors

    def _entries(self) -> dict[str, _Entry]:
        by_key: dict[str, _Entry] = {}
        for e in self._scan()[0]:
            by_key.setdefault(e.task.key, e)
        return by_key

    def all(self) -> list[Task]:
        """Every parsable task, in folder and file order. Use `validate()` to find the broken ones."""
        return [e.task for e in self._scan()[0]]

    def get(self, key: str) -> Task | None:
        entry = self._entries().get(key)
        return entry.task if entry else None

    def status(self, key: str) -> str | None:
        entry = self._entries().get(key)
        return entry.status if entry else None

    @staticmethod
    def _dep_done(dep: str, entries: dict[str, _Entry]) -> bool:
        entry = entries.get(KEY_PREFIX + dep)
        if entry is None:
            return _is_finished(dep, entries)
        return entry.status == "done"

    def deps_done(self, task: Task) -> bool:
        """Check that every dependency is `done` or finished (its file is gone, see `validate`)."""
        entries = self._entries()
        return all(self._dep_done(d, entries) for d in task.depends_on)

    def ready(self) -> list[Task]:
        """Tasks with status `ready` whose dependencies are all done."""
        entries = self._entries()
        return [
            e.task
            for e in entries.values()
            if e.status == "ready" and all(self._dep_done(d, entries) for d in e.task.depends_on)
        ]

    def validate(self) -> list[str]:
        """Check the whole set. Returns error messages; an empty list means every task file is sound."""
        entries, errors = self._scan()
        seen: dict[str, _Entry] = {}
        for e in entries:
            if e.task.key in seen:
                errors.append(f"duplicate id {e.task.id}: {seen[e.task.key].task.path} and {e.task.path}")
            seen.setdefault(e.task.key, e)
            if e.status not in STATUSES:
                errors.append(f"{e.task.path}: invalid status {e.status!r} (use one of {', '.join(STATUSES)})")
        for e in seen.values():
            errors.extend(
                f"{e.task.path}: {e.task.id} depends on unknown task {d}"
                for d in e.task.depends_on
                if KEY_PREFIX + d not in seen and not _is_finished(d, seen)
            )
        errors.extend(_cycle_errors(seen))
        return errors

    def mark_done(self, worktree: Path, key: str) -> str:
        """Delete the task's file under `worktree` and return its path: a finished file task is deleted, not marked."""
        entry = self._entries().get(key)
        if entry is None or entry.task.path is None:
            msg = f"unknown task {key}"
            raise KeyError(msg)
        (worktree / entry.task.path).unlink()
        return entry.task.path
