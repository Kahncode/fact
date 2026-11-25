"""
The review session's structured answer (`.fact/prompts/review.schema.json`): parse it, decide, and brief the fix.

Pure helpers, safe in workflow code. A review session that passed but whose answer does not parse is `failed_error`,
like a session that ended without a structured answer.
"""

from typing import Literal

from fact.core.activity_contract import CriterionResult
from fact.core.enums import StepOutcome
from fact.core.types import SUMMARY_MAX_BYTES, StepResult, limit_text
from fact.sdk.failures import CONTEXT_MAX_BYTES
from pydantic import BaseModel, ConfigDict, Field, ValidationError

Severity = Literal["low", "medium", "high"]
BLOCKING: frozenset[str] = frozenset({"medium", "high"})  # findings at these severities send the change back


class Finding(BaseModel):
    model_config = ConfigDict(frozen=True)

    severity: Severity
    file: str = ""
    line: int | None = None
    message: str


class Review(BaseModel):
    """What the review session answered: each acceptance criterion met or not, and its findings."""

    model_config = ConfigDict(frozen=True)

    criteria: list[CriterionResult] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)

    @property
    def unmet(self) -> list[CriterionResult]:
        return [c for c in self.criteria if not c.met]

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity in BLOCKING]

    @property
    def needs_changes(self) -> bool:
        """True when a criterion is unmet or a finding is medium or high: the change goes to `address_review`."""
        return bool(self.unmet or self.blocking)


def parse_review(result: StepResult) -> tuple[StepResult, Review | None]:
    """
    Return the review step's result and its parsed answer.

    A step that did not pass is returned as is, with no review. A passed step whose `criteria` or `findings` do not
    parse becomes `failed_error`.
    """
    if result.outcome != StepOutcome.PASSED:
        return result, None
    try:
        review = Review.model_validate({k: result.outputs.get(k) for k in ("criteria", "findings")})
    except ValidationError as exc:
        summary = limit_text(f"the review ended without a valid structured answer: {exc}", SUMMARY_MAX_BYTES)
        return result.model_copy(update={"outcome": StepOutcome.FAILED_ERROR, "summary": summary}), None
    return result, review


def _where(finding: Finding) -> str:
    if not finding.file:
        return ""
    return f"`{finding.file}:{finding.line}` " if finding.line is not None else f"`{finding.file}` "


def review_context(review: Review) -> str:
    """Build the text an `address_review` session gets: unmet criteria, then findings by severity, highest first."""
    parts: list[str] = []
    if review.unmet:
        lines = [f"- {c.text}" + (f"\n  Evidence: {c.evidence}" if c.evidence else "") for c in review.unmet]
        parts.append("### Unmet acceptance criteria\n\n" + "\n".join(lines))
    order = {"high": 0, "medium": 1, "low": 2}
    findings = sorted(review.findings, key=lambda f: order[f.severity])
    if findings:
        lines = [f"{i}. **{f.severity}** {_where(f)}{f.message}" for i, f in enumerate(findings, 1)]
        parts.append("### Findings (low ones are optional)\n\n" + "\n".join(lines))
    return limit_text("\n\n".join(parts), CONTEXT_MAX_BYTES)
