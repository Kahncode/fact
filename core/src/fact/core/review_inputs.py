"""
The inputs and results of the review activities: the wait for a PR's review, and the conversation steps of `converse`.

Re-exported by `fact.core.activity_contract`, like `fact.core.activity_inputs`.
"""

from datetime import datetime
from typing import Literal

from fact.core.types import PRRef
from pydantic import BaseModel, ConfigDict, Field

# what `wait_for_review` saw on a PR: a person merged or closed it, approved its head, or requested changes
ReviewEventKind = Literal["merged", "closed", "approved", "changes_requested"]
# a review's own body, an inline comment on the diff (`review_comment`), or a conversation comment (`comment`)
CommentKind = Literal["review", "review_comment", "comment"]


class _Input(BaseModel):
    model_config = ConfigDict(frozen=True)


class WaitForReviewInput(_Input):
    run_id: str
    prs: list[PRRef]  # the PRs still open; each is polled until something happens on it
    handled_reviews: list[int] = Field(default_factory=list)  # review ids the run acted on: never returned again
    # reviews submitted before this are ignored: on a PR an earlier run opened, earlier runs answered them
    reviews_since: datetime | None = None


class ReviewEvent(_Input):
    """The first thing that happened on one PR; `pr.head_sha` is the PR's head on GitHub when it was read."""

    pr: PRRef
    kind: ReviewEventKind
    review_id: int | None = None  # for `approved` and `changes_requested`
    reviewer: str = ""  # the review's author's login


class Comment(_Input):
    """A person's comment on a PR or an issue, as `collect_comments` read it."""

    id: int
    kind: CommentKind
    author: str
    body: str
    created_at: datetime
    url: str = ""
    path: str = ""  # an inline comment's file
    line: int | None = None  # and its line, when GitHub still has one
    review_id: int | None = None  # the review an inline comment belongs to; a review's own id for `review`
    in_reply_to: int | None = None  # the thread's first comment, for an inline reply


class Reply(_Input):
    """What a conversation session answers to one comment."""

    comment_id: int
    body: str


class CollectCommentsInput(_Input):
    run_id: str
    repo: str
    number: int  # a PR's or an issue's
    pull_request: bool = True  # False: an issue, which has only conversation comments
    since: datetime | None = None  # only comments made since; None: all of them
    review_id: int | None = None  # this review's body and inline comments are taken whatever `since` says


class PostRepliesInput(_Input):
    run_id: str
    repo: str
    number: int
    comments: list[Comment]  # what the session read: says where each reply goes
    replies: list[Reply]
    key: str  # one per conversation, so a retried attempt never posts a reply twice


class RequestReviewInput(_Input):
    run_id: str
    pr: PRRef
    reviewers: list[str]  # logins
