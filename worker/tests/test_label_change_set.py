"""`label_change_set`: a classified PR carries exactly one of the two labels and a `## Review` section."""

import json

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fact.core.activity_contract import LabelChangeSetInput, OpenChangeSetInput
from fact.core.classification import Classification
from fact.core.github import GitHubApp
from fact.core.types import PRRef, Task, WorkspaceHandle
from fact.worker.activities.change_set import ChangeSetActivities
from fact.worker.reporting import StepReporter
from temporalio.testing import ActivityEnvironment

REPO = "acme/widget"
PR = PRRef(repo=REPO, number=12, url=f"https://github.com/{REPO}/pull/12", head_sha="abc")
TOKEN = "ghs_label_secret_7"  # noqa: S105 - fake


@pytest.fixture(scope="module")
def private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


class FakeGitHub:
    """Keeps the PR's body and labels; removing a label the PR does not have is a 404, as on GitHub."""

    def __init__(self, labels: set[str]) -> None:
        self.body = ""
        self.labels = labels

    def handle(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911
        path, method = request.url.path, request.method
        issue = f"/repos/{REPO}/issues/12/labels"
        if path == f"/repos/{REPO}/installation":
            return httpx.Response(200, json={"id": 7, "account": {"login": "acme"}})
        if path == "/app/installations/7/access_tokens":
            return httpx.Response(201, json={"token": TOKEN, "expires_at": "2099-01-01T00:00:00Z"})
        if path == f"/repos/{REPO}":
            return httpx.Response(200, json={"default_branch": "main", "html_url": f"https://github.com/{REPO}"})
        if path == f"/repos/{REPO}/pulls/12" and method == "PATCH":
            self.body = json.loads(request.content)["body"]
            return httpx.Response(200, json={})
        if path == issue and method == "POST":
            self.labels |= set(json.loads(request.content)["labels"])
            return httpx.Response(200, json=[])
        if path.startswith(f"{issue}/") and method == "DELETE":
            name = path.removeprefix(f"{issue}/")
            if name not in self.labels:
                return httpx.Response(404, json={"message": "Label does not exist"})
            self.labels.remove(name)
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"message": "Not Found"})


def _spec(route: str) -> LabelChangeSetInput:
    handle = WorkspaceHandle(run_id="r1", root="/ws", repos={REPO: "/ws/widget"}, branches={REPO: "fact/x"})
    change_set = OpenChangeSetInput(
        run_id="r1",
        task=Task(key="task:M1-1", id="M1-1", title="Widget"),
        handle=handle,
        branch="fact/x",
        title="feat: widget",
        summary="Adds the widget.",
        classification=Classification(route=route, risk="low", door="two_way"),  # type: ignore[arg-type]
    )
    return LabelChangeSetInput(run_id="r1", change_set=change_set, prs=[PR])


@pytest.mark.parametrize(
    ("route", "before", "label"),
    [
        ("human_review", {"fact:auto-merge"}, "fact:needs-review"),  # a re-classified PR swaps its label
        ("auto_merge", set(), "fact:auto-merge"),
    ],
)
async def test_pr_gets_one_label_and_a_review_section(
    route: str, before: set[str], label: str, private_key: str, client: httpx.AsyncClient
) -> None:
    fake = FakeGitHub(set(before))
    github = GitHubApp(1, private_key, api_url="https://gh.test", transport=httpx.MockTransport(fake.handle))
    reporter = StepReporter("http://test", client)

    await ActivityEnvironment().run(ChangeSetActivities(github, reporter).label_change_set, _spec(route))

    assert fake.labels == {label}
    assert fake.body.startswith("Adds the widget.\n\n## Review\n\n")
    assert f"(`{label}`)" in fake.body
