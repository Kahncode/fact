"""The PR side of the `Standard` test fakes: push, CI, merge, base sync, review wait and replies, scripted per test."""

from dataclasses import dataclass, field

from fact.core.activity_contract import (
    CLASSIFY_SIGNALS,
    COLLECT_COMMENTS,
    LABEL_CHANGE_SET,
    MERGE_CHANGE_SET,
    OPEN_CHANGE_SET,
    POST_COST_COMMENT,
    POST_REPLIES,
    PUSH_BRANCH,
    REQUEST_REVIEW,
    SYNC_BASE,
    WAIT_FOR_CI,
    WAIT_FOR_REVIEW,
    ClassifySignalsInput,
    CollectCommentsInput,
    Comment,
    LabelChangeSetInput,
    MergeChangeSetInput,
    MergeResult,
    OpenChangeSetInput,
    PostCostCommentInput,
    PostRepliesInput,
    PushBranchInput,
    RequestReviewInput,
    ReviewEvent,
    ReviewEventKind,
    SyncBaseInput,
    WaitForCIInput,
    WaitForReviewInput,
)
from fact.core.classification import ClassifySignal
from fact.core.enums import StepOutcome
from fact.core.types import PRRef, StepResult
from standard_inputs import PR, REPO
from temporalio import activity
from temporalio.exceptions import ApplicationError

SyncScript = str  # `up_to_date`, `merged` or `conflicted`: one `sync_base` result


@dataclass
class FakePRSteps:
    """Scripted PR steps. Scripts are consumed in order; once empty, each falls back to its plain default."""

    broken: str | None = None  # name of an activity that raises a non-retryable error
    ci_failures: int = 0  # `wait_for_ci` steps that fail before one passes
    ci_conflicts: int = 0  # `wait_for_ci` steps that find the PR conflicting with its base, before the rest
    ci_timed_out: bool = False  # `wait_for_ci` gives up on a CI that never finishes
    fix_ci_commits: bool = True  # False: a `fix_ci` session that passes but commits nothing, so the head stays
    pr_state: ReviewEventKind = "merged"  # what `wait_for_review` sees on each PR once `review_script` is empty
    review_script: list[ReviewEventKind] = field(default_factory=list)  # first `wait_for_review` events, on the PR
    comments: list[Comment] = field(default_factory=list)  # what `collect_comments` reads
    ci_failures_after_approval: int = 0  # `wait_for_ci` failures once `wait_for_review` returned an approval
    merges: bool = True  # whether `merge_change_set` merges; False: GitHub refuses, so the run waits for a person
    merge_script: list[MergeResult] = field(default_factory=list)  # first `merge_change_set` results, before `merges`
    sync_script: list[SyncScript] = field(default_factory=list)  # first `sync_base` results, then `up_to_date`
    signals: list[ClassifySignal] = field(default_factory=list)  # what `classify_signals` raises
    calls: list[tuple[str, object]] = field(default_factory=list)
    merge_results: list[tuple[str, MergeResult]] = field(default_factory=list)
    queues: list[tuple[str, str]] = field(default_factory=list)  # (activity, the task queue it ran on)

    def _call(self, name: str, spec: object) -> None:
        self.calls.append((name, spec))
        self.queues.append((name, activity.info().task_queue))
        if name == self.broken:
            msg = f"{name} is broken"
            raise ApplicationError(msg, non_retryable=True)

    def of(self, name: str) -> list:
        return [arg for n, arg in self.calls if n == name]

    @activity.defn(name=PUSH_BRANCH)
    async def push_branch(self, spec: PushBranchInput) -> str:
        self._call(PUSH_BRANCH, spec)
        return "pushed"

    @activity.defn(name=OPEN_CHANGE_SET)
    async def open_change_set(self, spec: OpenChangeSetInput) -> list[PRRef]:
        self._call(OPEN_CHANGE_SET, spec)
        pushes = len(self.of(OPEN_CHANGE_SET))
        fixed = pushes - 1 if self.fix_ci_commits else 0
        return [PR if fixed == 0 else PR.model_copy(update={"head_sha": f"fix{fixed}"})]

    @activity.defn(name=POST_COST_COMMENT)
    async def post_cost_comment(self, spec: PostCostCommentInput) -> None:
        self._call(POST_COST_COMMENT, spec)

    @activity.defn(name=WAIT_FOR_CI)
    async def wait_for_ci(self, spec: WaitForCIInput) -> StepResult:
        self._call(WAIT_FOR_CI, spec)
        if self.ci_conflicts > 0:
            self.ci_conflicts -= 1
            outputs = {"checks": 0, "conflict": True}
            return StepResult(outcome=StepOutcome.FAILED_CHECKS, summary="conflicts with its base", outputs=outputs)
        if self.ci_timed_out:
            outputs = {"timed_out": True}
            return StepResult(outcome=StepOutcome.FAILED_CHECKS, summary="CI still running", outputs=outputs)
        if self.ci_failures > 0:
            self.ci_failures -= 1
            failure = {"name": "test (windows)", "conclusion": "failure", "url": "https://ci/1", "log_tail": "E boom"}
            return StepResult(outcome=StepOutcome.FAILED_CHECKS, summary="1 failed", outputs={"failures": [failure]})
        return StepResult(outcome=StepOutcome.PASSED, summary="CI passed")

    @activity.defn(name=WAIT_FOR_REVIEW)
    async def wait_for_review(self, spec: WaitForReviewInput) -> list[ReviewEvent]:
        """See a PR `merge_change_set` merged as merged; else the next scripted event, on the PR's current head."""
        self._call(WAIT_FOR_REVIEW, spec)
        merged = any(n == MERGE_CHANGE_SET and r == "merged" for n, r in self.merge_results)
        kind: ReviewEventKind = (
            "merged" if merged else self.review_script.pop(0) if self.review_script else self.pr_state
        )
        review_id = 100 + len(self.of(WAIT_FOR_REVIEW)) if kind in {"approved", "changes_requested"} else None
        if kind == "approved":
            self.ci_failures += self.ci_failures_after_approval
        return [ReviewEvent(pr=pr, kind=kind, review_id=review_id, reviewer="octo") for pr in spec.prs]

    @activity.defn(name=COLLECT_COMMENTS)
    async def collect_comments(self, spec: CollectCommentsInput) -> list[Comment]:
        self._call(COLLECT_COMMENTS, spec)
        return self.comments

    @activity.defn(name=POST_REPLIES)
    async def post_replies(self, spec: PostRepliesInput) -> int:
        self._call(POST_REPLIES, spec)
        return len(spec.replies)

    @activity.defn(name=REQUEST_REVIEW)
    async def request_review(self, spec: RequestReviewInput) -> None:
        self._call(REQUEST_REVIEW, spec)

    @activity.defn(name=MERGE_CHANGE_SET)
    async def merge_change_set(self, spec: MergeChangeSetInput) -> MergeResult:
        self._call(MERGE_CHANGE_SET, spec)
        result: MergeResult = self.merge_script.pop(0) if self.merge_script else "merged" if self.merges else "refused"
        self.merge_results.append((MERGE_CHANGE_SET, result))
        return result

    @activity.defn(name=SYNC_BASE)
    async def sync_base(self, spec: SyncBaseInput) -> StepResult:
        self._call(SYNC_BASE, spec)
        state = self.sync_script.pop(0) if self.sync_script else "up_to_date"
        if state == "conflicted":
            outputs = {"merged": [], "conflicts": {REPO: ["docs/ARCHITECTURE.md"]}}
            return StepResult(outcome=StepOutcome.FAILED_CHECKS, summary="1 files conflict", outputs=outputs)
        outputs = {"merged": [REPO] if state == "merged" else [], "conflicts": {}}
        return StepResult(outcome=StepOutcome.PASSED, summary=state, outputs=outputs)

    @activity.defn(name=CLASSIFY_SIGNALS)
    async def classify_signals(self, spec: ClassifySignalsInput) -> list[ClassifySignal]:
        self._call(CLASSIFY_SIGNALS, spec)
        return self.signals

    @activity.defn(name=LABEL_CHANGE_SET)
    async def label_change_set(self, spec: LabelChangeSetInput) -> None:
        self._call(LABEL_CHANGE_SET, spec)
