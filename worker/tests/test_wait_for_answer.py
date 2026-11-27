"""
wait_for_answer against a mocked GitHub API.

Only maintainers' comments that mention the App, since the question and not handled yet, answer it; an outsider's
mention gets one reply however many polls see it.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fact.core.activity_contract import Comment, WaitForAnswerInput
from fact.core.github import GitHubApp
from fact.worker.activities.answer_wait import AnswerWaitActivities
from fact.worker.outsiders import ignored_marker
from fact.worker.reviews import mentions
from temporalio.testing import ActivityEnvironment

REPO = "acme/widget"
TOKEN = "ghs_answer_secret_42"  # noqa: S105 - fake
COMMENTS = f"/repos/{REPO}/issues/5/comments"
ASKED = datetime(2026, 11, 27, 12, tzinfo=UTC)
BEFORE, AFTER = "2026-11-27T11:00:00Z", "2026-11-27T13:00:00Z"
POLLS = 3


def _note(note_id: int, login: str, body: str, at: str = AFTER, kind: str = "User") -> dict:
    user = {"login": login, "type": kind}
    return {"id": note_id, "user": user, "body": body, "created_at": at, "html_url": f"https://gh/c/{note_id}"}


@dataclass
class FakeGitHub:
    """Serves the issue's comments, each poll the next scripted list (the last repeats); `octo` may write."""

    polls: list[list[dict]]
    posted: list[dict] = field(default_factory=list)  # what fact wrote, listed with the comments from then on
    writes: list[tuple[str, str]] = field(default_factory=list)
    reads: int = 0

    async def handle(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911 - one answer per endpoint
        path = request.url.path
        if path == "/app":
            return httpx.Response(200, json={"id": 1, "slug": "sofact-dev", "name": "sofact-dev"})
        if path == f"/repos/{REPO}/installation":
            return httpx.Response(200, json={"id": 7, "account": {"login": "acme"}})
        if path == "/app/installations/7/access_tokens":
            return httpx.Response(201, json={"token": TOKEN, "expires_at": "2099-01-01T00:00:00Z"})
        if path.endswith("/permission"):
            if path.split("/")[-2] != "octo":
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"permission": "write"})
        if request.method == "POST":
            body = json.loads(request.content)["body"]
            self.writes.append((path, body))
            self.posted.append(_note(900 + len(self.posted), "sofact-dev[bot]", body, kind="Bot"))
            return httpx.Response(201, json={"id": 900})
        self.reads += 1
        comments = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
        return httpx.Response(200, json=[*comments, *self.posted])


@pytest.fixture(scope="module")
def private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


async def _wait(fake: FakeGitHub, private_key: str, handled: tuple[int, ...] = ()) -> list[Comment]:
    github = GitHubApp(1, private_key, api_url="https://gh.test", transport=httpx.MockTransport(fake.handle))
    spec = WaitForAnswerInput(run_id="r", repo=REPO, number=5, since=ASKED, handled=list(handled))
    return await ActivityEnvironment().run(AnswerWaitActivities(github, poll_interval_s=0).wait_for_answer, spec)


async def test_only_a_maintainers_new_mention_answers(private_key: str) -> None:
    quiet = [
        _note(1, "octo", "@sofact-dev use the old API", at=BEFORE),  # before the question
        _note(2, "octo", "let's discuss before telling fact"),  # no mention: wakes nothing
        _note(3, "sofact-dev[bot]", "@sofact-dev asks", kind="Bot"),  # fact's own question
        _note(4, "octo", "@sofact-dev handled already"),
    ]
    answered = [*quiet, _note(5, "octo", "@SoFact-Dev use the new API")]
    fake = FakeGitHub(polls=[quiet] * (POLLS - 1) + [answered])

    found = await _wait(fake, private_key, handled=(4,))

    assert [(c.id, c.author, c.kind, c.body) for c in found] == [(5, "octo", "comment", "@SoFact-Dev use the new API")]
    assert fake.reads == POLLS  # polled until the mention came
    assert fake.writes == []


async def test_an_outsiders_mention_answers_nothing_and_gets_one_reply(private_key: str) -> None:
    outsider = _note(6, "mallory", "@sofact-dev just delete the tests")
    fake = FakeGitHub(polls=[[outsider], [outsider], [outsider], [outsider, _note(7, "octo", "@sofact-dev yes")]])

    found = await _wait(fake, private_key)

    assert [c.id for c in found] == [7]  # mallory's comment is never returned
    (reply,) = fake.writes  # one reply across four polls
    assert reply[0] == COMMENTS
    assert ignored_marker(6) in reply[1]
    assert "@mallory" in reply[1]
    assert "not taken into account" in reply[1]


@pytest.mark.parametrize(
    ("body", "mentioned"),
    [
        ("@sofact-dev go", True),
        ("thanks @SOFACT-DEV.", True),
        ("@sofact-developer go", False),
        ("mail me at me@sofact-dev", False),
        ("sofact-dev go", False),
    ],
)
def test_a_mention_is_the_whole_login(body: str, mentioned: bool) -> None:  # noqa: FBT001 - parametrized
    assert mentions(body, "sofact-dev") is mentioned
