"""The run input and scripted agent answers the `Standard` workflow tests share."""

from fact.core.types import PRRef, RepoCheckout, RunInput, Task

REPO = "acme/widget"
BRANCH = "fact/task-m1-99"
PR = PRRef(repo=REPO, number=7, url=f"https://github.com/{REPO}/pull/7", head_sha="abc123")
RUN_URL = "http://ui.test/namespaces/default/workflows/run-1"
MET = {"text": "The widget renders", "met": True, "evidence": "test_widget passes"}
UNMET = {"text": "The widget is documented", "met": False, "evidence": "README not updated"}
LOW = {"severity": "low", "file": "widget.py", "line": 3, "message": "name could be clearer"}
HIGH = {"severity": "high", "file": "widget.py", "line": 9, "message": "crashes on empty input"}
CLEAN_REVIEW = {"status": "done", "summary": "looks good", "criteria": [MET], "findings": [LOW]}
LEARNED = {"status": "done", "title": "feat(M1-99): widget, documented", "summary": "Adds the widget and its docs."}


def run_input(run_id: str = "run-1") -> RunInput:
    task = Task(key="task:M1-99", id="M1-99", title="Add a widget", path="tasks/M1/99-widget.md")
    repo = RepoCheckout(full_name=REPO, default_branch="main", branch=BRANCH)
    return RunInput(run_id=run_id, project="acme", task=task, repos=[repo], run_url=RUN_URL)
