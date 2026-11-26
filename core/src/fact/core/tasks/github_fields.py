"""
Reading a GitHub issue's fields: type, complexity, acceptance criteria, `Depends on` lines, done-ness and its key.

Used by `GitHubIssuesSource`; pure functions on the issue JSON as the REST API returns it.
"""

import re
from typing import Any, get_args

from fact.core.tasks.base import GITHUB_PREFIX
from fact.core.types import Complexity, TaskType

READY_LABEL = "fact:ready"
EPIC_TYPE = "epic"
TYPE_ALIASES: dict[str, TaskType] = {"incident": "bug"}  # an incident runs the bug workflow
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_CRITERION_RE = re.compile(r"^\s*[-*+]\s*\[[ xX]\]\s*(.+?)\s*$")
_DEPENDS_RE = re.compile(r"^\s*(?:[-*+]\s*)?\**depends on\**:?\**\s*(.+)$", re.IGNORECASE)
_REF_RE = re.compile(r"(?<![\w/.-])(?:([A-Za-z0-9-]+/[A-Za-z0-9._-]+))?#([1-9]\d*)\b")


def acceptance_criteria(body: str) -> list[str]:
    """Checklist items under an "Acceptance criteria" heading of any level, up to the next heading."""
    found: list[str] = []
    in_section = False
    for line in body.splitlines():
        if m := _HEADING_RE.match(line):
            in_section = m.group(1).strip().rstrip(":").lower() == "acceptance criteria"
        elif in_section and (m := _CRITERION_RE.match(line)):
            found.append(m.group(1))
    return found


def depends_on_lines(body: str, repo: str) -> list[str]:
    """Keys from `Depends on #3, owner/other#4` lines; a bare `#n` is an issue of `repo`."""
    keys: list[str] = []
    for line in body.splitlines():
        if m := _DEPENDS_RE.match(line):
            keys.extend(f"{GITHUB_PREFIX}{r or repo}#{n}" for r, n in _REF_RE.findall(m.group(1)))
    return keys


def label_names(issue: dict[str, Any]) -> list[str]:
    return [str(label["name"] if isinstance(label, dict) else label) for label in issue.get("labels") or []]


def _label_value(labels: list[str], group: str) -> str | None:
    prefix = f"{group}:"
    return next((n[len(prefix) :].strip().lower() for n in labels if n.lower().startswith(prefix)), None)


def has_label(issue: dict[str, Any], name: str) -> bool:
    return name.lower() in (n.lower() for n in label_names(issue))


def is_epic(issue: dict[str, Any]) -> bool:
    """Tell whether the issue is an epic (`type:epic`): a parent with no work of its own, never run."""
    return _label_value(label_names(issue), "type") == EPIC_TYPE


def task_type(labels: list[str]) -> TaskType:
    value = _label_value(labels, "type") or "feature"
    value = TYPE_ALIASES.get(value, value)
    return value if value in get_args(TaskType) else "feature"  # type: ignore[return-value]


def complexity(labels: list[str]) -> Complexity:
    value = _label_value(labels, "complexity")
    return value if value in get_args(Complexity) else "standard"  # type: ignore[return-value]


def is_done(issue: dict[str, Any]) -> bool:
    """
    Tell whether the issue is closed as completed; one closed as not planned or duplicate is not done.

    A closed issue without a reason (closed before GitHub recorded one) is done; a PR is done once merged.
    """
    if (pr := issue.get("pull_request")) is not None:
        return bool(pr.get("merged_at"))
    return issue.get("state") == "closed" and issue.get("state_reason") in {"completed", None}


def issue_key(issue: dict[str, Any], default_repo: str) -> str:
    repo = (issue.get("repository") or {}).get("full_name")
    if not repo and (url := issue.get("repository_url")):
        repo = "/".join(str(url).rstrip("/").split("/")[-2:])
    return f"{GITHUB_PREFIX}{repo or default_repo}#{issue['number']}"


def unique_keys(keys: list[str]) -> list[str]:
    """Drop repeated keys (GitHub names are case-insensitive), keeping the first spelling and the order."""
    out: list[str] = []
    seen: set[str] = set()
    for key in keys:
        if key.lower() not in seen:
            seen.add(key.lower())
            out.append(key)
    return out
