"""
`Standard` asks on the issue: a blocked agent's question is posted there, and maintainers' mentions of the App answer it.

Each `wait_for_answer` that returns new comments starts one `answer` session: `continue` resumes the blocked session,
`ask` posts its reply and waits again, `stop` ends the run; `fact gate` answers too, and wins.
"""

from datetime import UTC, datetime

from fact.core.activity_contract import (
    AGENT_STEP,
    COLLECT_COMMENTS,
    POST_REPLIES,
    RECORD_GATE_ANSWERED,
    RECORD_GATE_OPENED,
    REPORT_STATUS,
    WAIT_FOR_ANSWER,
    Comment,
)
from fact.core.enums import RunOutcome
from fact.core.gates import GateDecision
from fact.core.types import RunResult
from gate_harness import answer as answer_gate
from gate_harness import until_status
from standard_harness import FakeActivities, finished, run_standard, started_standard
from standard_inputs import ISSUE, REPO, issue_run_input
from standard_question_fakes import answer, blocked

QUESTION = "Use the v1 or the v2 widget API?"
AT = datetime(2026, 11, 27, 13, tzinfo=UTC)
TWICE = 2


def _mention(comment_id: int, body: str) -> Comment:
    return Comment(id=comment_id, kind="comment", author="octo", body=f"@sofact-dev {body}", created_at=AT)


def _sessions(fakes: FakeActivities, prompt: str) -> list:
    return [s for s in fakes.of(AGENT_STEP) if s.prompt_name == prompt]


def _events(fakes: FakeActivities) -> list[str]:
    return [r.event for r in fakes.of(REPORT_STATUS)]


async def test_continue_answers_the_gate_and_resumes_the_session_with_the_thread() -> None:
    thread = [_mention(41, "v2, please")]
    fakes = FakeActivities(
        implement_script=[blocked(QUESTION)],
        issue_answers=[thread],
        comments=thread,
        answers=[answer("continue", instructions="Use the v2 API.")],
        gate_decisions=None,  # the issue answers
    )
    result, _ = await run_standard(fakes, "run-ask-continue", issue_run_input())

    assert result.outcome == RunOutcome.MERGED
    opened = next(r for r in fakes.of(REPORT_STATUS) if r.event == "gate_opened")
    assert QUESTION in opened.detail
    assert "@sofact-dev" in opened.detail
    assert _events(fakes).count("gate_answered") == 1  # removes `fact:needs-human`
    (gate,) = fakes.gates(RECORD_GATE_OPENED)
    (wait,) = fakes.of(WAIT_FOR_ANSWER)
    assert (wait.repo, wait.number, wait.since, wait.handled) == (REPO, ISSUE, gate.opened_at, [])
    (collect,) = fakes.of(COLLECT_COMMENTS)
    assert (collect.pull_request, collect.mentioning_app, collect.since) == (False, True, gate.opened_at)
    (session,) = _sessions(fakes, "answer")
    assert QUESTION in session.context
    assert "v2, please" in session.context
    (answered,) = fakes.gates(RECORD_GATE_ANSWERED)
    assert (answered.decision, answered.note) == ("approve", "Use the v2 API.")
    _, resumed = _sessions(fakes, "implement")
    assert resumed.resume_session_id == "s-1"
    assert "Use the v2 API." in resumed.context
    assert "v2, please" in resumed.context  # the thread
    assert fakes.of(POST_REPLIES) == []


async def test_ask_posts_its_reply_and_waits_again_then_stop_ends_the_run() -> None:
    first, second = _mention(41, "v1?"), _mention(42, "stop, we drop this")
    fakes = FakeActivities(
        implement_script=[blocked(QUESTION)],
        issue_answers=[[first], [second]],
        comments=[first, second],
        answers=[answer("ask", reply="v1 is deprecated: v1 anyway?"), answer("stop")],
        gate_decisions=None,
    )
    result, _ = await run_standard(fakes, "run-ask-stop", issue_run_input())

    assert result.outcome == RunOutcome.NEEDS_HUMAN
    assert QUESTION in result.summary
    assert finished(fakes) == RunOutcome.NEEDS_HUMAN
    assert len(_sessions(fakes, "answer")) == TWICE  # one per wait that found new comments
    (posted,) = fakes.of(POST_REPLIES)
    assert [(r.comment_id, r.body) for r in posted.replies] == [(42, "v1 is deprecated: v1 anyway?")]
    assert [w.handled for w in fakes.of(WAIT_FOR_ANSWER)] == [[], [41, 42]]
    (answered,) = fakes.gates(RECORD_GATE_ANSWERED)
    assert answered.decision == "reject"
    assert len(_sessions(fakes, "implement")) == 1  # never resumed


async def test_fact_gate_wins_over_the_issue() -> None:
    fakes = FakeActivities(implement_script=[blocked(QUESTION)], gate_decisions=None)  # nobody answers on the issue
    async with started_standard(fakes, "run-ask-gate", issue_run_input()) as (_, handle):
        # the gate is open, and the issue is watched meanwhile
        status = await until_status(handle, lambda s: s.gate is not None and bool(fakes.of(WAIT_FOR_ANSWER)))
        assert status.gate is not None
        gate = status.gate
        await answer_gate(handle, gate.gate_id, GateDecision.APPROVE, "v2")
        result: RunResult = await handle.result()

    assert result.outcome == RunOutcome.MERGED
    assert _sessions(fakes, "answer") == []
    assert len(fakes.of(WAIT_FOR_ANSWER)) == 1
    _, resumed = _sessions(fakes, "implement")
    assert (resumed.resume_session_id, "v2" in resumed.context) == ("s-1", True)
