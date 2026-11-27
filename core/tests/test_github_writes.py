"""GitHub Issues write-back against a fake GitHub: each run event, the native status, and the context section."""

import json
from dataclasses import dataclass, field

import httpx
import pytest
from fact.core.activity_contract import ReportStatusInput, TaskEvent
from fact.core.github import github_client
from fact.core.holds import HELD_ENDS
from fact.core.tasks import GitHubIssuesSource, TaskStatus
from fact.core.tasks.events import NEEDS_HUMAN, RUNNING, report
from fact.core.tasks.github_fields import READY_LABEL
from fact.core.tasks.github_writes import CONTEXT_END, CONTEXT_START, with_context
from fact.core.types import PRRef

KEY = "gh:acme/app#7"
ISSUE = "/repos/acme/app/issues/7"
PR = PRRef(repo="acme/app", number=9, url="https://github.com/acme/app/pull/9", head_sha="abc")
PR2 = PRRef(repo="acme/lib", number=3, url="https://github.com/acme/lib/pull/3", head_sha="def")
SECTION = f"{CONTEXT_START}\nctx\n{CONTEXT_END}"
NO_STATUSES = "no statuses"  # the repository has no native statuses: the REST issue has no `status` field
STATUSES = ["Backlog", "Todo", "In progress", "In review", "Done", "Not planned", "Duplicate"]


@dataclass
class FakeGitHub:
    """
    One issue: records every write as (method, path, json), and keeps the body, labels and status as GitHub would.

    `status` is the issue's native status name (None: no status); a `setIssueStatus` mutation is recorded as
    ("setIssueStatus", status name, None).
    """

    body: str | None = ""
    labels: set[str] = field(default_factory=set)
    status: str | None = NO_STATUSES
    writes: list[tuple[str, str, object]] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        payload = json.loads(request.content) if request.content else None
        if request.method == "GET":
            found: dict[str, object] = {"number": 7, "node_id": "I_7", "body": self.body}
            if self.status != NO_STATUSES:
                found["status"] = {"name": self.status} if self.status else None
            return httpx.Response(200, json=found)
        if path == "/graphql":
            return self._graphql(payload)
        self.writes.append((request.method, path, payload))
        if request.method == "DELETE":
            name = path.rsplit("/", 1)[1]
            if name not in self.labels:
                return httpx.Response(404, json={"message": "Label does not exist"})
            self.labels.discard(name)
        elif path.endswith("/labels"):
            self.labels.update(payload["labels"])
        elif request.method == "PATCH" and "body" in payload:
            self.body = payload["body"]
        return httpx.Response(200, json={})

    def _graphql(self, payload: dict) -> httpx.Response:
        if "planningStatuses" in payload["query"]:
            nodes = [{"id": f"S_{name}", "name": name} for name in STATUSES]
            return httpx.Response(200, json={"data": {"repository": {"planningStatuses": {"nodes": nodes}}}})
        assert payload["variables"]["issueId"] == "I_7"
        self.status = payload["variables"]["statusId"].removeprefix("S_")
        self.writes.append(("setIssueStatus", self.status, None))
        return httpx.Response(200, json={"data": {"setIssueStatus": {"clientMutationId": None}}})


@dataclass(frozen=True)
class _Token:
    token: str


@dataclass
class FakeApp:
    github: FakeGitHub
    token_repos: list[str] = field(default_factory=list)

    async def installation_token(self, full_name: str) -> _Token:
        self.token_repos.append(full_name)
        return _Token(f"tok-{full_name}")

    def api_client(self, token: str | None) -> httpx.AsyncClient:
        return github_client(token, api_url="https://gh.test", transport=httpx.MockTransport(self.github.handle))


def _source(github: FakeGitHub) -> GitHubIssuesSource:
    return GitHubIssuesSource("acme/app", FakeApp(github))  # type: ignore[arg-type]


async def _report(github: FakeGitHub, event: TaskEvent, **kw: object) -> list[tuple[str, str, object]]:
    spec = ReportStatusInput(run_id="r1", project="acme", task_key=KEY, event=event, **kw)  # type: ignore[arg-type]
    await report(_source(github), spec)
    return github.writes


async def test_run_started_labels_running_and_links_the_run() -> None:
    github = FakeGitHub()
    writes = await _report(github, "run_started", run_url="http://ui/runs/r1")
    assert writes[0] == ("POST", f"{ISSUE}/labels", {"labels": [RUNNING]})
    assert writes[1][:2] == ("POST", f"{ISSUE}/comments")
    assert "http://ui/runs/r1" in writes[1][2]["body"]  # type: ignore[index]


async def test_gate_opened_asks_for_a_person_and_gate_answered_clears_it() -> None:
    github = FakeGitHub(labels={RUNNING})
    opened = await _report(github, "gate_opened", detail="approve the plan")
    assert opened[0] == ("POST", f"{ISSUE}/labels", {"labels": [NEEDS_HUMAN]})
    assert opened[1][:2] == ("POST", f"{ISSUE}/comments")
    assert "approve the plan" in opened[1][2]["body"]  # type: ignore[index]
    github.writes.clear()
    assert await _report(github, "gate_answered") == [("DELETE", f"{ISSUE}/labels/fact:needs-human", None)]
    assert github.labels == {RUNNING}


async def test_prs_opened_comments_the_links() -> None:
    writes = await _report(FakeGitHub(), "prs_opened", prs=[PR, PR2])
    ((method, path, payload),) = writes
    assert (method, path) == ("POST", f"{ISSUE}/comments")
    assert PR.url in payload["body"]  # type: ignore[index]
    assert PR2.url in payload["body"]  # type: ignore[index]


async def test_no_prs_posts_nothing() -> None:
    assert await _report(FakeGitHub(), "prs_opened") == []


async def test_merged_closes_as_completed_and_clears_running() -> None:
    github = FakeGitHub(labels={RUNNING})
    writes = await _report(github, "merged")
    assert writes[0] == ("PATCH", ISSUE, {"state": "closed", "state_reason": "completed"})
    assert github.labels == set()  # `fact:needs-human` was not there: its 404 is not an error


@pytest.mark.parametrize("event", ["stopped", "needs_human", "pr_opened", "closed"])
async def test_a_held_end_adds_needs_human_keeps_ready_and_says_how_to_release(event: TaskEvent) -> None:
    github = FakeGitHub(labels={READY_LABEL, RUNNING})
    writes = await _report(github, event, detail="no longer wanted")
    assert github.labels == {READY_LABEL, NEEDS_HUMAN}
    assert writes[-1][:2] == ("POST", f"{ISSUE}/comments")
    body = writes[-1][2]["body"]  # type: ignore[index]
    assert f"ended {event}: no longer wanted" in body
    assert HELD_ENDS[event] in body
    assert f"remove the `{NEEDS_HUMAN}` label" in body
    assert f"`fact run {KEY}`" in body


@pytest.mark.parametrize(
    ("event", "before", "after"),
    [
        ("run_started", None, "In progress"),
        ("run_started", "Backlog", "In progress"),
        ("run_started", "Todo", "In progress"),
        ("run_started", "In progress", "In progress"),
        ("run_started", "In review", "In review"),
        ("run_started", "Done", "Done"),
        ("prs_opened", None, "In review"),
        ("prs_opened", "Todo", "In review"),
        ("prs_opened", "In progress", "In review"),
        ("prs_opened", "In review", "In review"),
        ("prs_opened", "Done", "Done"),
        ("prs_opened", "Not planned", "Not planned"),
        ("prs_opened", "Duplicate", "Duplicate"),
    ],
)
async def test_a_run_moves_the_status_only_forward(event: TaskEvent, before: str | None, after: str) -> None:
    github = FakeGitHub(status=before)
    writes = await _report(github, event, prs=[PR])
    assert github.status == after
    assert [w for w in writes if w[0] == "setIssueStatus"] == (
        [] if before == after else [("setIssueStatus", after, None)]
    )


@pytest.mark.parametrize("event", ["gate_opened", "gate_answered", "merged", "stopped", "needs_human"])
async def test_other_events_leave_the_status(event: TaskEvent) -> None:
    github = FakeGitHub(status="Todo")
    await _report(github, event)
    assert github.status == "Todo"


async def test_a_repository_without_statuses_is_skipped() -> None:
    github = FakeGitHub()  # the issue has no `status` field
    writes = await _report(github, "run_started")
    assert [w[:2] for w in writes] == [("POST", f"{ISSUE}/labels"), ("POST", f"{ISSUE}/comments")]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404, json={"message": "Not Found"}),
        httpx.Response(500, json={"message": "boom"}),
        httpx.Response(200, json={"errors": [{"type": "NOT_FOUND", "message": "no planningStatuses"}]}),
        httpx.Response(200, json={"errors": [{"message": "Field 'setIssueStatus' doesn't exist"}]}),
    ],
)
async def test_a_failed_status_write_never_raises(response: httpx.Response) -> None:
    github = FakeGitHub(status="Todo")
    handle = github.handle
    github.handle = lambda request: response if request.url.path == "/graphql" else handle(request)  # type: ignore[method-assign]
    await _source(github).advance_status(KEY, TaskStatus.in_progress)
    assert github.status == "Todo"


async def test_write_context_appends_markers_to_a_body_without_them() -> None:
    github = FakeGitHub(body="My text.\r\n\r\n- [ ] keep me")
    await _source(github).write_context(KEY, "Run r1: implementing.")
    assert github.body == f"My text.\r\n\r\n- [ ] keep me\n\n{CONTEXT_START}\nRun r1: implementing.\n{CONTEXT_END}\n"
    await _source(github).write_context(KEY, "Run r1: PR open.")  # replaces, does not append again
    assert github.body == f"My text.\r\n\r\n- [ ] keep me\n\n{CONTEXT_START}\nRun r1: PR open.\n{CONTEXT_END}\n"


async def test_write_context_on_an_empty_body_and_unchanged_body() -> None:
    github = FakeGitHub(body=None)
    await _source(github).write_context(KEY, "ctx")
    assert github.body == f"{SECTION}\n"
    github.writes.clear()
    await _source(github).write_context(KEY, "ctx")
    assert github.writes == []  # nothing changed: no PATCH


def test_text_outside_the_markers_is_kept_byte_for_byte() -> None:
    before = "  Intro\r\n\t*with* odd   spacing  é—\n\n"
    after = "\r\n## Mine\r\ntrailing spaces   \n\n\n"
    body = f"{before}{CONTEXT_START}\nold\ncontext\n{CONTEXT_END}{after}"
    updated = with_context(body, "new")
    assert updated == f"{before}{CONTEXT_START}\nnew\n{CONTEXT_END}{after}"
    assert updated.encode().startswith(before.encode())
    assert updated.encode().endswith(f"{CONTEXT_END}{after}".encode())


@pytest.mark.parametrize(
    "body",
    [
        f"Mine.\n{CONTEXT_START}\nperson's text after a lone start",  # start without end
        f"Mine.\n{CONTEXT_END}\nmore of mine",  # end without start
        f"Mine.\n{CONTEXT_END}\nmine\n{CONTEXT_START}\nstill mine",  # end before start
    ],
)
def test_broken_markers_are_left_alone_and_a_pair_is_appended(body: str) -> None:
    updated = with_context(body, "ctx")
    assert updated == f"{body}\n\n{SECTION}\n"
    assert with_context(updated, "ctx2") == f"{body}\n\n{CONTEXT_START}\nctx2\n{CONTEXT_END}\n"  # stable afterwards


def test_one_pair_among_text_and_a_section_holding_markers() -> None:
    body = f"a\n{CONTEXT_START}\nx\n{CONTEXT_END}\nb"
    assert with_context(body, f"y {CONTEXT_END} {CONTEXT_START}") == f"a\n{CONTEXT_START}\ny  \n{CONTEXT_END}\nb"


async def test_writes_use_the_key_repository_token_and_skip_other_keys() -> None:
    github = FakeGitHub()
    app = FakeApp(github)
    source = GitHubIssuesSource("acme/app", app)  # type: ignore[arg-type]
    await source.comment("gh:acme/other#3", "hi")
    assert app.token_repos == ["acme/other"]
    assert github.writes == [("POST", "/repos/acme/other/issues/3/comments", {"body": "hi"})]
    github.writes.clear()
    await source.comment("task:M1-01", "hi")  # a file task on a GitHub-sourced project: nothing to write
    await source.set_labels("task:M1-01", [RUNNING], [])
    await source.write_context("task:M1-01", "ctx")
    await source.close("task:M1-01")
    await source.advance_status("task:M1-01", TaskStatus.in_progress)
    assert github.writes == []
