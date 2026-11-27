"""Parent issues and sub-issues in the GitHub source: the parent's label gates, open sub-issues are dependencies."""

from fact.core.tasks.github_issues import PARENT_CLOSED
from fake_github_issues import FakeGitHub, closed, issue, source

READY = "fact:ready"
PLANNED = "fact:planned"


def _keys(items: list) -> list[str]:
    return [i.task.key for i in items]


async def test_a_sub_issue_waits_for_its_parent_to_be_ready() -> None:
    github = FakeGitHub()
    github.add(issue(1, labels=(PLANNED,)))
    github.add(issue(2, labels=(READY,)), parent=1)
    assert await source(github).list_ready() == []
    held = await source(github).list_ready(include_waiting=True)
    assert [(i.task.key, i.gated_by) for i in held] == [("gh:acme/app#2", "gh:acme/app#1")]

    github.issues["acme/app", 1]["labels"] = [{"name": READY}]
    # the parent is ready now, but waits on its open sub-issue; the sub-issue runs
    assert _keys(await source(github).list_ready()) == ["gh:acme/app#2"]


async def test_a_grandchild_waits_on_its_grandparent() -> None:
    github = FakeGitHub()
    github.add(issue(1, labels=(PLANNED,)))
    github.add(issue(2, labels=(READY,)), parent=1)
    github.add(issue(3, labels=(READY,)), parent=2)
    src = source(github)
    assert await src.list_ready() == []
    assert await src.not_ready_ancestor("gh:acme/app#3") == "gh:acme/app#1"

    github.issues["acme/app", 1]["labels"] = [{"name": READY}]
    assert _keys(await source(github).list_ready()) == ["gh:acme/app#3"]


async def test_open_sub_issues_hold_their_parent() -> None:
    github = FakeGitHub()
    github.add(issue(1, labels=(READY, "type:feature")))
    github.add(issue(2), parent=1)  # open, not even ready: it still holds the parent
    github.add(closed(3, reason="not_planned"), parent=1)
    items = await source(github).list_ready(include_waiting=True)
    assert [(i.task.key, i.waiting_on) for i in items] == [("gh:acme/app#1", ("gh:acme/app#2",))]
    assert await source(github).list_ready() == []

    github.issues["acme/app", 2].update(state="closed", state_reason="duplicate")  # any close reason will do
    ready = await source(github).list_ready()
    assert _keys(ready) == ["gh:acme/app#1"]
    assert ready[0].task.depends_on == []


async def test_a_finished_parent_without_a_work_type_is_closed_never_listed() -> None:
    github = FakeGitHub()
    github.add(issue(1, labels=(READY, "type:epic")))
    github.add(closed(2), parent=1)
    github.add(issue(3, labels=(READY,)))  # no type label at all: a parent without a work type too
    github.add(closed(4), parent=3)
    github.add(issue(5, labels=(READY, "type:epic")))  # open sub-issue: stays open
    github.add(issue(6), parent=5)
    assert await source(github).list_ready(include_waiting=True) == []
    assert github.comments == []  # a read without close_parents (the preview) writes nothing
    assert github.issues["acme/app", 1]["state"] == "open"

    assert await source(github).list_ready(close_parents=True) == []
    assert github.comments == [(1, PARENT_CLOSED), (3, PARENT_CLOSED)]
    for n in (1, 3):
        assert (github.issues["acme/app", n]["state"], github.issues["acme/app", n]["state_reason"]) == (
            "closed",
            "completed",
        )
    assert github.issues["acme/app", 5]["state"] == "open"


async def test_a_parent_runs_by_its_work_type_whatever_the_label_order() -> None:
    github = FakeGitHub()
    github.add(issue(1, labels=(READY, "type:epic", "type:chore")))
    github.add(closed(2), parent=1)
    github.add(issue(3, labels=(READY, "type:chore", "type:epic")))
    github.add(closed(4), parent=3)
    github.add(issue(5, labels=(READY,)))  # a leaf without a type label runs as a feature
    ready = await source(github).list_ready(close_parents=True)
    assert [(i.task.key, i.task.type) for i in ready] == [
        ("gh:acme/app#1", "chore"),
        ("gh:acme/app#3", "chore"),
        ("gh:acme/app#5", "feature"),
    ]
    assert github.comments == []


async def test_a_parent_under_a_planned_parent_stays_open() -> None:
    github = FakeGitHub()
    github.add(issue(1, labels=(PLANNED,)))
    github.add(issue(2, labels=(READY, "type:epic")), parent=1)
    github.add(closed(3), parent=2)
    assert await source(github).list_ready(close_parents=True) == []
    assert github.comments == []
    assert github.issues["acme/app", 2]["state"] == "open"


async def test_one_parent_request_per_issue_per_read() -> None:
    github = FakeGitHub()
    github.add(issue(1, labels=(READY,), sub_issues_summary={"total": 2, "completed": 0}))
    github.add(issue(2, labels=(READY,), sub_issues_summary={"total": 0, "completed": 0}), parent=1)
    github.add(issue(3, labels=(READY,), sub_issues_summary={"total": 0, "completed": 0}), parent=1)
    github.add(issue(4, labels=(READY,), sub_issues_summary={"total": 0, "completed": 0}))
    src = source(github)
    assert _keys(await src.list_ready()) == ["gh:acme/app#2", "gh:acme/app#3", "gh:acme/app#4"]
    for key in ("gh:acme/app#1", "gh:acme/app#2", "gh:acme/app#3", "gh:acme/app#4"):
        assert await src.not_ready_ancestor(key) is None
    parents = github.paths("/parent")
    assert sorted(parents) == ["/repos/acme/app/issues/1/parent", "/repos/acme/app/issues/4/parent"]
    # the sub-issues listing gave #2 and #3 their parent; only #1 said it has sub-issues
    assert github.paths("/sub_issues") == ["/repos/acme/app/issues/1/sub_issues"]


async def test_file_keys_have_no_ancestor() -> None:
    assert await source(FakeGitHub()).not_ready_ancestor("task:M1-01") is None


async def test_a_blocker_on_the_parent_holds_its_sub_issue() -> None:
    github = FakeGitHub()
    github.add(issue(1, labels=(READY,)))
    github.add(issue(2, labels=(READY,)), parent=1)
    github.add(issue(9))
    github.blocked_by[1] = [github.issues["acme/app", 9]]
    items = await source(github).list_ready(include_waiting=True)
    # the parent #1 has no work type, so it is never listed
    assert [(i.task.key, i.waiting_on) for i in items] == [("gh:acme/app#2", ("gh:acme/app#9",))]
    assert await source(github).list_ready() == []

    github.issues["acme/app", 9].update(state="closed", state_reason="completed")
    assert _keys(await source(github).list_ready()) == ["gh:acme/app#2"]


async def test_a_grandparent_blocker_and_a_parent_depends_on_line_hold_the_grandchild() -> None:
    github = FakeGitHub()
    github.add(issue(1, labels=(READY,)))
    github.add(issue(2, labels=(READY,), body="Depends on #8\n"), parent=1)
    github.add(issue(3, labels=(READY,)), parent=2)
    github.add(issue(8))
    github.add(issue(9))
    github.blocked_by[1] = [github.issues["acme/app", 9]]
    src = source(github)
    items = {i.task.key: i.waiting_on for i in await src.list_ready(include_waiting=True)}
    assert items["gh:acme/app#3"] == ("gh:acme/app#8", "gh:acme/app#9")
    assert sorted(github.paths("/blocked_by")) == [
        f"/repos/acme/app/issues/{n}/dependencies/blocked_by" for n in (1, 2, 3)
    ]

    github.issues["acme/app", 9].update(state="closed", state_reason="completed")
    assert await source(github).list_ready() == []  # #8 still holds it through #2's line
    github.issues["acme/app", 8].update(state="closed", state_reason="completed")
    assert _keys(await source(github).list_ready()) == ["gh:acme/app#3"]


async def test_a_parents_open_sub_issues_do_not_hold_their_siblings() -> None:
    github = FakeGitHub()
    github.add(issue(1, labels=(READY,)))
    github.add(issue(2, labels=(READY,)), parent=1)
    github.add(issue(3), parent=1)  # open, not ready
    ready = await source(github).list_ready()
    assert _keys(ready) == ["gh:acme/app#2"]
    assert ready[0].task.depends_on == []
