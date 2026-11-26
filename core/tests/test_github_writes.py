"""GitHub Issues write-back against a fake GitHub: each run event, and the context section between its markers."""

import json
from dataclasses import dataclass, field

import httpx
import pytest
from fact.core.activity_contract import ReportStatusInput, TaskEvent
from fact.core.github import github_client
from fact.core.tasks import GitHubIssuesSource
from fact.core.tasks.events import NEEDS_HUMAN, RUNNING, report
from fact.core.tasks.github_writes import CONTEXT_END, CONTEXT_START, with_context
from fact.core.types import PRRef

KEY = "gh:acme/app#7"
ISSUE = "/repos/acme/app/issues/7"
PR = PRRef(repo="acme/app", number=9, url="https://github.com/acme/app/pull/9", head_sha="abc")
PR2 = PRRef(repo="acme/lib", number=3, url="https://github.com/acme/lib/pull/3", head_sha="def")
SECTION = f"{CONTEXT_START}\nctx\n{CONTEXT_END}"


@dataclass
class FakeGitHub:
    """One issue: records every write as (method, path, json), and keeps the body and labels as GitHub would."""

    body: str | None = ""
    labels: set[str] = field(default_factory=set)
    writes: list[tuple[str, str, object]] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        payload = json.loads(request.content) if request.content else None
        if request.method == "GET":
            return httpx.Response(200, json={"number": 7, "body": self.body})
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
    assert writes == [
        ("POST", f"{ISSUE}/labels", {"labels": [RUNNING]}),
        ("POST", f"{ISSUE}/comments", {"body": "fact started run [r1](http://ui/runs/r1)."}),
    ]


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


@pytest.mark.parametrize("event", ["closed", "stopped"])
async def test_closed_or_stopped_clears_running_and_comments(event: TaskEvent) -> None:
    github = FakeGitHub(labels={RUNNING, NEEDS_HUMAN})
    writes = await _report(github, event, detail="no longer wanted")
    assert github.labels == set()
    assert writes[-1][:2] == ("POST", f"{ISSUE}/comments")
    assert f"ended {event}: no longer wanted" in writes[-1][2]["body"]  # type: ignore[index]


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
    assert github.writes == []
