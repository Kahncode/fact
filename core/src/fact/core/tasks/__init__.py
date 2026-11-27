"""
Task sources: where fact reads its work from. Callers use `source_for` and the `TaskSource` protocol.

`FileTaskSource` (`fact.core.tasks.files`) is not exported: only this package and tests import it (import-linter).
"""

from fact.core.tasks.base import (
    ReadyItem,
    TaskKey,
    TaskSource,
    branch_name,
    dependency_key,
    parse_key,
    source_for,
)
from fact.core.tasks.github_issues import GitHubIssuesSource

__all__ = [
    "GitHubIssuesSource",
    "ReadyItem",
    "TaskKey",
    "TaskSource",
    "branch_name",
    "dependency_key",
    "parse_key",
    "source_for",
]
