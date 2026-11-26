"""Task sources: where fact reads its work from. Callers use `source_for` and the `TaskSource` protocol."""

from fact.core.tasks.base import (
    ReadyItem,
    TaskKey,
    TaskSource,
    branch_name,
    dependency_key,
    parse_key,
    source_for,
)
from fact.core.tasks.files import FileTaskSource
from fact.core.tasks.github_issues import GitHubIssuesSource

__all__ = [
    "FileTaskSource",
    "GitHubIssuesSource",
    "ReadyItem",
    "TaskKey",
    "TaskSource",
    "branch_name",
    "dependency_key",
    "parse_key",
    "source_for",
]
