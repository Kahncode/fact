"""
What classify decides for a change set, and what it decides from (`06-workflow-authoring.md`, Classify).

`classify_signals` raises `ClassifySignal`s with no LLM; a read-only agent session rates risk, door and security; the
workflow combines them into a `Classification`, which labels the PRs and fills their `## Review` section.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# a reason to want a person's review that `classify_signals` finds with no LLM
SignalName = Literal["schema_change", "api_change", "api_breaking", "always_review", "task_closed"]
Risk = Literal["high", "low"]
Door = Literal["one_way", "two_way"]  # a one-way door is hard to undo
ReviewRoute = Literal["auto_merge", "human_review"]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class ChangedPath(_Model):
    repo: str  # full name, a key of the workspace's repositories
    path: str  # relative to the repository, with `/`


class ClassifySignal(_Model):
    """A signal `classify_signals` raised, with the changed paths that raised it (none for `task_closed`)."""

    name: SignalName
    paths: list[ChangedPath] = Field(default_factory=list)


class SecurityFinding(_Model):
    """A security problem the classify session found in the change."""

    file: str = ""
    line: int | None = None
    message: str  # what is wrong and why it matters


class Classification(_Model):
    """
    How a change set was classified: its route, every reason that forces a review, and what it was decided from.

    `risk` and `door` are None when the classify session gave no valid answer; `reasons` then says so.
    """

    route: ReviewRoute
    reasons: list[str] = Field(default_factory=list)  # why `human_review`; empty for `auto_merge`
    signals: list[ClassifySignal] = Field(default_factory=list)
    risk: Risk | None = None
    risk_reason: str = ""
    door: Door | None = None
    door_reason: str = ""
    security_findings: list[SecurityFinding] = Field(default_factory=list)
