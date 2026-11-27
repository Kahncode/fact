"""
Classify's pure half: brief the read-only classify session, parse its answer and decide the change set's route.

Safe in workflow code. A change set goes to `human_review` when any signal holds (`06-workflow-authoring.md`,
Classify): the spec was ambiguous, a security finding, a schema change, an API change or break, an always-review
path, the task closed during the run, high risk, a one-way door; and when the classify session gave no valid answer,
so nothing unjudged is merged. Otherwise `auto_merge`. Assumptions alone never force a review.
"""

from collections.abc import Sequence

from fact.core.classification import Classification, ClassifySignal, Door, Risk, SecurityFinding, SignalName
from fact.core.enums import StepOutcome
from fact.core.types import StepResult, limit_text
from fact.sdk.failures import CONTEXT_MAX_BYTES
from fact.sdk.review import Review, review_context
from pydantic import BaseModel, ConfigDict, Field, ValidationError

SIGNAL_REASONS: dict[SignalName, str] = {
    "schema_change": "changes the database schema",
    "api_change": "changes the API surface",
    "api_breaking": "breaks the published API",
    "always_review": "touches a path that is always reviewed",
    "task_closed": "the task was closed during the run",
}
MAX_LISTED_PATHS = 5  # per signal, in a reason


class ClassifyAnswer(BaseModel):
    """The classify session's structured answer (`.fact/prompts/classify.schema.json`)."""

    model_config = ConfigDict(frozen=True)

    risk: Risk
    risk_reason: str
    door: Door
    door_reason: str
    security_findings: list[SecurityFinding] = Field(default_factory=list)


def parse_classify(result: StepResult) -> ClassifyAnswer | str:
    """Return the session's answer, or why there is none usable: it did not pass, or its answer does not parse."""
    if result.outcome != StepOutcome.PASSED:
        return f"the classify session ended {result.outcome}: {result.summary}".strip()
    keys = ("risk", "risk_reason", "door", "door_reason", "security_findings")
    try:
        return ClassifyAnswer.model_validate({k: result.outputs[k] for k in keys if k in result.outputs})
    except ValidationError as exc:
        return f"the classify session gave no valid answer: {exc.error_count()} errors"


def _paths(signal: ClassifySignal) -> str:
    shown = [f"`{p.path}`" for p in signal.paths[:MAX_LISTED_PATHS]]
    more = len(signal.paths) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


def signal_reason(signal: ClassifySignal) -> str:
    """One line on a signal, with the paths that raised it, such as ``changes the API surface: `api/openapi.json` ``."""
    text = SIGNAL_REASONS[signal.name]
    return f"{text}: {_paths(signal)}" if signal.paths else text


def decide(
    signals: Sequence[ClassifySignal],
    answer: ClassifyAnswer | str,
    *,
    spec_ambiguous: bool,
    problems: Sequence[str] = (),
) -> Classification:
    """
    Combine the deterministic signals and the session's answer into the change set's route.

    `answer` is the session's answer, or why it gave none; `problems` are other reasons the classification is
    incomplete (such as signals that could not be read). Either forces `human_review`.
    """
    reasons = list(problems)
    if spec_ambiguous:
        reasons.append("the implementation found the spec ambiguous")
    reasons += [signal_reason(s) for s in signals]
    if isinstance(answer, str):
        reasons.append(answer)
        values = {}
    else:
        values = answer.model_dump()
        if answer.security_findings:
            reasons.append(f"{len(answer.security_findings)} security finding(s)")
        if answer.risk == "high":
            reasons.append(f"high risk: {answer.risk_reason}")
        if answer.door == "one_way":
            reasons.append(f"a one-way door: {answer.door_reason}")
    route = "human_review" if reasons else "auto_merge"
    return Classification(route=route, reasons=reasons, signals=list(signals), **values)


def classify_context(
    review: Review | None, signals: Sequence[ClassifySignal], assumptions: Sequence[str], *, spec_ambiguous: bool
) -> str:
    """Build the classify session's `$context`: the review's result, the assumptions and the deterministic signals."""
    parts = []
    reviewed = review_context(review) if review is not None else ""
    if review is not None and review.criteria:
        met = "\n".join(f"- {'met' if c.met else 'NOT met'}: {c.text}" for c in review.criteria)
        reviewed = f"### Acceptance criteria as reviewed\n\n{met}" + (f"\n\n{reviewed}" if reviewed else "")
    parts.append(f"## Review result\n\n{reviewed or 'The review found nothing to change.'}")
    lines = ["The implementation found the spec ambiguous."] if spec_ambiguous else []
    lines += [f"- {a}" for a in assumptions]
    parts.append("## Assumptions\n\n" + ("\n".join(lines) or "None."))
    found = "\n".join(f"- `{s.name}`: {signal_reason(s)}" for s in signals)
    parts.append(f"## Signals from the rules\n\n{found or 'None.'}")
    return limit_text("\n\n".join(parts), CONTEXT_MAX_BYTES)
