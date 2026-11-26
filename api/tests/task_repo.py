"""A git checkout whose `origin/main` holds task files, for the tests that read tasks from the default branch."""

import subprocess
from pathlib import Path

PROJECT = "acme"
PROJECT_YAML = """name: acme
repositories:
  - full_name: acme/home
    role: backend
task_source:
  kind: files
  path: tasks
workflows:
  default: standard
"""


def task_file(task_id: str, deps: list[str], status: str = "ready", complexity: str = "standard") -> str:
    return (
        f"---\nid: {task_id}\ntitle: Task {task_id}\ntype: feature\ncomplexity: {complexity}\nstatus: {status}\n"
        f"depends_on: [{', '.join(deps)}]\nhuman_actions: []\n---\n\n## Goal\n\nDo it.\n\n"
        "## Acceptance criteria\n\n- [ ] it works\n"
    )


def git(cwd: Path, *args: str) -> None:
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", *ident, *args], cwd=cwd, check=True, capture_output=True)  # noqa: S603, S607


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def make_checkout(tmp_path: Path) -> Path:
    """Clone a bare origin; origin/main holds M1-01 (done), M1-02 (needs M1-01) and M1-03 (needs M1-02)."""
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", str(origin), str(work))
    git(work, "checkout", "-b", "main")
    write(
        work,
        {
            ".fact/project.yaml": PROJECT_YAML,
            "tasks/M1/01-one.md": task_file("M1-01", [], status="done"),
            "tasks/M1/02-two.md": task_file("M1-02", ["M1-01"], complexity="complex"),
            "tasks/M1/03-three.md": task_file("M1-03", ["M1-02"]),
        },
    )
    git(work, "add", ".")
    git(work, "commit", "-m", "tasks")
    git(work, "push", "origin", "main")
    # Only in the working tree: a start must not see it.
    write(work, {"tasks/M1/04-local.md": task_file("M1-04", [])})
    return work
