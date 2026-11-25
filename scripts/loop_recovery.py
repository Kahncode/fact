"""
Recovery state for task_loop.py: survive usage limits, crashes and restarts without losing or trusting cut-off work.

The loop records the task it is running in `.tmp/task-loop/state.json` before each run, and whether any run of it
hit the usage limit. With that, a restart (even during a sleep for the usage reset) can tell:

- the task committed, but a run hit the limit: the commit may be incomplete, so the task file is restored with a
  `resume` field and a Resume section (see `reopen_task`), and it runs again first to verify and finish the work;
- the task committed cleanly: nothing to do;
- the tree holds uncommitted work and nothing was committed since: if the task is still the top runnable one, it
  runs again and continues from that work (its task file is restored if the run deleted it but did not commit);
  otherwise the loop stops, as for any dirty tree.
"""

# ruff: noqa: INP001, S603, S607
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / ".tmp" / "task-loop"
STATE_FILE = STATE_DIR / "state.json"
RESUME_NOTE = (
    "A previous run of this task was cut off (usage limit, crash or loop restart) before it committed. "
    "Its partial work is in the working tree: review it against the task, keep what is right, fix what is not, "
    "and finish every step of the task, including the checks, the review and the commit."
)
_RESUME_FIELD_RE = re.compile(r"^resume:.*\n", re.MULTILINE)
_RESUME_SECTION_RE = re.compile(r"\n## Resume\n.*?(?=\n## |\Z)", re.DOTALL)


def git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def tree_is_clean(cwd: Path = ROOT) -> bool:
    return not git("status", "--porcelain", cwd=cwd)


@dataclass
class LoopState:
    task_id: str
    task_path: str  # relative to the repo root, posix
    head_before: str  # HEAD when the task's first run started
    hit_limit: bool = False  # some run of this task hit the usage limit
    reopens: int = 0  # times the task was reopened in a row, a guard against reopening forever

    def save(self, path: Path = STATE_FILE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path = STATE_FILE) -> LoopState | None:
        try:
            return cls(**json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError, ValueError, TypeError:
            return None

    @staticmethod
    def clear(path: Path = STATE_FILE) -> None:
        path.unlink(missing_ok=True)


class Recovery(StrEnum):
    NONE = "none"  # nothing left over: run tasks as usual
    REOPEN = "reopen"  # committed after a run hit the limit: restore the task to verify the commit
    RESUME = "resume"  # uncommitted work of the task in the tree: run it again on top of that work
    DIRTY = "dirty"  # uncommitted work the loop can't attribute: stop


def decide(state: LoopState | None, *, head: str, clean: bool, task_file_exists: bool, is_top_task: bool) -> Recovery:
    """Choose what to do at start-up with the state the previous loop left (pure; the caller reads git and files)."""
    if state is None:
        return Recovery.NONE if clean else Recovery.DIRTY
    if head != state.head_before:  # something was committed since the task started
        if not clean:
            return Recovery.DIRTY
        return Recovery.REOPEN if state.hit_limit and not task_file_exists else Recovery.NONE
    if clean:
        return Recovery.NONE
    return Recovery.RESUME if task_file_exists and is_top_task else Recovery.DIRTY


def restore_task_file(state: LoopState, *, root: Path = ROOT) -> bool:
    """
    Bring back a task file the run deleted but did not commit (cut off during its final commit step).

    Without it the next run could not find its task. True if the file was restored.
    """
    path = root / state.task_path
    if path.exists() or git("rev-parse", "HEAD", cwd=root) != state.head_before:
        return False
    git("checkout", state.head_before, "--", state.task_path, cwd=root)
    return True


def add_resume(text: str, commits: str) -> str:
    """Return the task file text with `resume: "<commits>"` in its front matter and a Resume section after it."""
    _, front, body = text.split("---", 2)
    front = _RESUME_FIELD_RE.sub("", front.lstrip("\n"))
    body = _RESUME_SECTION_RE.sub("", body)
    section = (
        "\n## Resume\n\n"
        f"A run of this task hit the Claude usage limit, so its commits `{commits}` may be incomplete or wrong. "
        "Check them against every step of the task and every acceptance criterion as if they were someone else's "
        "work: run the checks and the review, finish or fix what is missing, update the context, then delete this "
        "file and commit as usual (the commit may hold only the fixes and this deletion)."
    )
    return f'---\n{front}resume: "{commits}"\n---\n{section}{body}'


def reopen_task(task_path: str, head_before: str, *, root: Path = ROOT) -> Path:
    """
    Restore a task file deleted by suspect commits, marked for verification, and commit it.

    The range starts at `head_before`, or where an earlier reopen's range started, so no unverified commit is lost.
    Raises `subprocess.CalledProcessError` (with git's stderr) when the commit fails, for example in a hook.
    """
    text = git("show", f"{head_before}:{task_path}", cwd=root) + "\n"
    earlier = _RESUME_FIELD_RE.search(text.split("---", 2)[1])
    start = earlier.group(0).split(":", 1)[1].strip().strip("\"'").split("..")[0] if earlier else ""
    start = start or git("rev-parse", "--short", head_before, cwd=root)
    commits = f"{start}..{git('rev-parse', '--short', 'HEAD', cwd=root)}"
    path = root / task_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(add_resume(text, commits), encoding="utf-8", newline="\n")
    match = re.search(r"^id:\s*(\S+)", text, re.MULTILINE)
    task_id = match.group(1).strip("\"'") if match else path.stem
    git("add", "--", task_path, cwd=root)
    git("commit", "-m", f"chore(tasks): reopen {task_id} to verify {commits} after the usage limit", cwd=root)
    return path
