"""
`converse`, a library step as a mixin of `FactWorkflow`: answer people's comments on a PR or an issue.

Collect the people's comments since a point (`collect_comments`, a control activity), run a read-only agent session
with a prompt whose schema adds `decision`, `replies` (`{comment_id, body}`) or one `reply` (to the latest comment),
and `instructions`, post the replies it wrote as `fact[bot]` (`post_replies`: in their thread for an inline review comment, a PR or issue comment otherwise)
and return its decision. The session holds no GitHub token: it only reads the workspace; fact posts for it. Threads
are never resolved. A session that does not pass, or whose answer does not parse or names a decision not offered,
posts nothing; replies that cannot be posted are logged, and the decision stands.

`ConverseSteps` uses the workflow's `input`, `agent`, `_attempts` and `_activity`.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from temporalio import workflow
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from fact.core.activity_contract import (
        COLLECT_COMMENTS,
        POST_REPLIES,
        CollectCommentsInput,
        Comment,
        PostRepliesInput,
        Reply,
    )
    from fact.core.enums import StepOutcome
    from fact.core.types import SUMMARY_MAX_BYTES, StepResult, limit_text
    from fact.sdk.failures import CONTEXT_MAX_BYTES

if TYPE_CHECKING:
    from fact.sdk.workflow import FactWorkflow

COMMENT_MAX_CHARS = 4000  # of one comment, in the session's context


class ConverseAnswer(BaseModel):
    """What a conversation session decided, the replies it wrote and its instructions for what comes next."""

    model_config = ConfigDict(frozen=True)

    decision: str
    replies: list[Reply] = Field(default_factory=list)
    reply: str = ""  # one reply, to the latest comment, for a prompt that answers a thread as a whole
    instructions: str = ""

    def to_post(self, comments: Sequence[Comment]) -> list[Reply]:
        """Return the replies to post: `replies`, else `reply` to the latest comment read (none if it read none)."""
        if self.replies or not self.reply.strip() or not comments:
            return list(self.replies)
        return [Reply(comment_id=comments[-1].id, body=self.reply)]


class Conversation(BaseModel):
    """A `converse` step: the session's step, its parsed answer (None if it gave none usable) and what it read."""

    model_config = ConfigDict(frozen=True)

    step: StepResult
    answer: ConverseAnswer | None = None
    comments: list[Comment] = Field(default_factory=list)

    @property
    def decision(self) -> str | None:
        return self.answer.decision if self.answer else None


def _comment_text(comment: Comment) -> str:
    where = f" on `{comment.path}`" + (f" line {comment.line}" if comment.line else "") if comment.path else ""
    thread = f", replying to {comment.in_reply_to}" if comment.in_reply_to else ""
    body = comment.body if len(comment.body) <= COMMENT_MAX_CHARS else comment.body[:COMMENT_MAX_CHARS] + "…"
    head = f"#### Comment {comment.id} ({comment.kind}) by @{comment.author}{where}{thread}, {comment.created_at:%Y-%m-%d %H:%M} UTC"
    return f"{head}\n\n{body}"


def comments_text(comments: Sequence[Comment]) -> str:
    """Render the comments, oldest first, each under a heading with its id, kind, author and place."""
    return "\n\n".join(_comment_text(c) for c in comments) or "No comments."


def parse_conversation(result: StepResult, decisions: Sequence[str]) -> tuple[StepResult, ConverseAnswer | None]:
    """Return the step and its answer; a passed step whose answer does not parse or is not offered is `failed_error`."""
    if result.outcome != StepOutcome.PASSED:
        return result, None
    try:
        keys = ("decision", "replies", "reply", "instructions")
        answer = ConverseAnswer.model_validate({k: result.outputs[k] for k in keys if k in result.outputs})
    except ValidationError as exc:
        problem = f"the session ended without a valid structured answer: {exc.error_count()} errors"
    else:
        if answer.decision in decisions:
            return result, answer
        problem = f"the session decided {answer.decision!r}, not one of {', '.join(decisions)}"
    failed = result.model_copy(
        update={"outcome": StepOutcome.FAILED_ERROR, "summary": limit_text(problem, SUMMARY_MAX_BYTES)}
    )
    return failed, None


class ConverseSteps:
    async def converse(  # noqa: PLR0913 - where to read, from when, and what the session may decide
        self: FactWorkflow,
        stage_id: str,
        prompt: str,
        *,
        repo: str,
        number: int,
        decisions: Sequence[str],
        pull_request: bool = True,
        since: datetime | None = None,
        review_id: int | None = None,
        mentioning_app: bool = False,
        context: str = "",
    ) -> Conversation:
        """
        Read the people's comments on PR (or issue) `number` since `since`, answer them in one session, post its replies.

        `context` goes before the comments in the session's `$context`. With `review_id`, that review's body and
        comments are read whatever `since` says. With `mentioning_app`, only the comments that mention the App.
        """
        run_id = self.input.run_id
        spec = CollectCommentsInput(
            run_id=run_id,
            repo=repo,
            number=number,
            pull_request=pull_request,
            since=since,
            review_id=review_id,
            mentioning_app=mentioning_app,
        )
        comments = await self._activity(COLLECT_COMMENTS, spec, list[Comment])
        brief = "\n\n".join(p for p in (context, f"## Comments\n\n{comments_text(comments)}") if p)
        step = await self.agent(stage_id, prompt, limit_text(brief, CONTEXT_MAX_BYTES), pr_text=False, ask=False)
        step, answer = parse_conversation(step, decisions)
        if answer is not None and (replies := answer.to_post(comments)):
            key = f"{run_id}:{stage_id}-{self._attempts.get(stage_id, 0)}"
            post = PostRepliesInput(
                run_id=run_id, repo=repo, number=number, comments=comments, replies=replies, key=key
            )
            try:
                await self._activity(POST_REPLIES, post, int)
            except ActivityError as exc:  # the decision still stands; a person sees the replies are missing
                workflow.logger.warning("%s: posting the replies failed: %s", stage_id, exc.cause or exc)
        return Conversation(step=step, answer=answer, comments=comments)
