"""
A task held for a person: the `fact:needs-human` label, and the words that say why and how to release it.

An issue the scheduler will not start for a reason only a person can lift carries `fact:needs-human` (kept with
`fact:ready`). Run ends that hold the task add it (`fact.core.tasks.events`), and so does the scheduler's tick when a
task reaches `concurrency.max_failed_runs` failed runs in a row (`fact.api.failure_cap`); each also sends one
`task_held` notification. A person releases it by removing the label, or with `fact run`, which removes it first.

Plain strings only, so workflow code (`fact.sdk`) can import this module.
"""

NEEDS_HUMAN = "fact:needs-human"

# why each run end the scheduler does not retry holds its task
HELD_ENDS: dict[str, str] = {
    "stopped": "its run was stopped",
    "needs_human": "its run needs a person",
    "pr_opened": "its run ended with a PR open for review",
    "closed": "its PR was closed without merging",
}


def failure_cap_reason(failed: int) -> str:
    return f"its last {failed} runs failed in a row"


def release_hint(task_key: str) -> str:
    """How a person releases the task: remove the label (issues only) or start it with `fact run`."""
    run = f"start it with `fact run {task_key}`"
    return f"remove the `{NEEDS_HUMAN}` label, or {run}" if task_key.startswith("gh:") else run


def held_text(task_key: str, reason: str) -> str:
    """Say why the task is held and how to release it: the comment and the notification body."""
    return f"The scheduler will not start this task again: {reason}. To release it, {release_hint(task_key)}."


def held_title(task_key: str, reason: str) -> str:
    return f"{task_key} is held: {reason}"
