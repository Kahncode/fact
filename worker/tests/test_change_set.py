"""open_change_set: push to a local bare remote, create or reuse the PR against a mocked GitHub API."""

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fact.core.github import GitHubApp
from fact.core.settings import Settings
from fact.core.types import RepoCheckout, Task, WorkspaceHandle, WorkspaceSpec
from fact.worker import git_push
from fact.worker.activities.change_set import ChangeSetActivities, OpenChangeSetInput
from fact.worker.activities.workspace import WorkspaceActivities
from fact.worker.git_push import auth_header
from fact.worker.pr_body import CheckResult
from fact.worker.reporting import StepReporter
from fact.worker.runners import LocalWorktreeRunner
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

FULL_NAME = "acme/widget"
BRANCH = "fact/task-m1-11"
TOKEN = "ghs_changeset_secret_42"  # noqa: S105 - fake
BOT_ID = 4321
PR_NUMBER = 12


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)  # noqa: S603, S607
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@dataclass
class FakeGitHub:
    open_prs: list[dict] = field(default_factory=list)
    requests: list[tuple[str, str, dict | None]] = field(default_factory=list)
    reject_create: bool = False  # answer 422 like GitHub does for a branch with no commits

    async def handle(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        self.requests.append((method, path, body))
        if path == "/app":
            return httpx.Response(200, json={"id": 1, "slug": "fact", "name": "fact"})
        if path == "/users/fact[bot]":
            return httpx.Response(200, json={"id": BOT_ID})
        if path == f"/repos/{FULL_NAME}/installation":
            return httpx.Response(200, json={"id": 7, "account": {"login": "acme"}})
        if path == "/app/installations/7/access_tokens":
            return httpx.Response(201, json={"token": TOKEN, "expires_at": "2099-01-01T00:00:00Z"})
        if path == f"/repos/{FULL_NAME}":
            return httpx.Response(200, json={"default_branch": "main", "html_url": f"https://github.com/{FULL_NAME}"})
        if path == f"/repos/{FULL_NAME}/pulls" and method == "GET":
            assert (request.url.params["head"], request.url.params["base"]) == (f"acme:{BRANCH}", "main")
            return httpx.Response(200, json=self.open_prs)
        if path == f"/repos/{FULL_NAME}/pulls" and method == "POST" and self.reject_create:
            return httpx.Response(422, json={"message": "No commits between main and fact/task-m1-11"})
        if path == f"/repos/{FULL_NAME}/pulls" and method == "POST":
            pr = {"number": 12, "html_url": f"https://github.com/{FULL_NAME}/pull/12"}
            self.open_prs.append(pr)
            return httpx.Response(201, json=pr)
        if path == f"/repos/{FULL_NAME}/pulls/12" and method == "PATCH":
            return httpx.Response(200, json=self.open_prs[0])
        return httpx.Response(404, json={"message": "Not Found"})


@pytest.fixture
def fake() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture(scope="module")
def private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


@pytest.fixture
def github(fake: FakeGitHub, private_key: str) -> GitHubApp:
    return GitHubApp(1, private_key, api_url="https://gh.test", transport=httpx.MockTransport(fake.handle))


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    bare, seed = tmp_path / "remote.git", tmp_path / "seed"
    _git("init", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    _git("clone", str(bare), str(seed), cwd=tmp_path)
    _git("config", "user.name", "seed", cwd=seed)
    _git("config", "user.email", "seed@example.com", cwd=seed)
    (seed / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", ".", cwd=seed)
    _git("commit", "-m", "init", cwd=seed)
    _git("push", "origin", "HEAD:main", cwd=seed)
    return bare


@pytest.fixture
def reporter(client: httpx.AsyncClient, tmp_path: Path) -> StepReporter:
    return StepReporter("http://test", client, log_dir=tmp_path / "logs")


async def _prepare(settings: Settings, remote: Path, reporter: StepReporter, run_id: str, **kw: object) -> Path:
    runner = LocalWorktreeRunner(settings, remote_url=lambda _n: remote.as_uri(), **kw)
    spec = WorkspaceSpec(
        run_id=run_id,
        task_key="task:M1-11",
        repos=[RepoCheckout(full_name=FULL_NAME, default_branch="main", branch=BRANCH)],
    )
    handle = await ActivityEnvironment().run(WorkspaceActivities(runner, reporter).prepare_workspace, spec)
    return Path(handle.repos[FULL_NAME])


def _commit(worktree: Path, name: str) -> None:
    (worktree / name).write_text("change\n", encoding="utf-8")
    _git("add", ".", cwd=worktree)
    _git("commit", "-m", f"add {name}", cwd=worktree)


def _input(run_id: str, worktree: Path, branch: str = BRANCH) -> OpenChangeSetInput:
    task = Task(key="task:M1-11", id="M1-11", title="Open the PR", acceptance_criteria=["It works"], path="t.md")
    return OpenChangeSetInput(
        run_id=run_id,
        task=task,
        handle=WorkspaceHandle(run_id=run_id, root=str(worktree.parent), repos={FULL_NAME: str(worktree)}),
        branch=branch,
        title="Open the PR",
        summary="Pushes and opens.",
        check_results=[CheckResult(name="test", outcome="passed")],
        run_url="http://fact.test/runs/1",
    )


async def test_creates_then_reuses_pr(  # noqa: PLR0913, PLR0917
    settings: Settings,
    remote: Path,
    reporter: StepReporter,
    run_id: str,
    github: GitHubApp,
    fake: FakeGitHub,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argvs: list[list[str]] = []
    real_spawn = git_push.spawn

    async def recording_spawn(argv, cwd, env, **kw):  # noqa: ANN001, ANN003, ANN202
        argvs.append(list(argv))
        return await real_spawn(argv, cwd, env, **kw)

    monkeypatch.setattr(git_push, "spawn", recording_spawn)
    worktree = await _prepare(settings, remote, reporter, run_id, github=github)
    assert _git("config", "user.name", cwd=worktree) == "fact[bot]"
    assert _git("config", "user.email", cwd=worktree) == f"{BOT_ID}+fact[bot]@users.noreply.github.com"
    _commit(worktree, "one.txt")
    activities = ChangeSetActivities(github, reporter, remote_url=lambda _n: remote.as_uri())

    (pr,) = await ActivityEnvironment().run(activities.open_change_set, _input(run_id, worktree))
    assert (pr.repo, pr.number, pr.url) == (FULL_NAME, 12, "https://github.com/acme/widget/pull/12")
    assert pr.head_sha == _git("rev-parse", f"refs/heads/{BRANCH}", cwd=remote)
    (created,) = [b for m, p, b in fake.requests if m == "POST" and p.endswith("/pulls")]
    assert (created["head"], created["base"], created["title"]) == (BRANCH, "main", "Open the PR")
    assert created["body"].startswith("Pushes and opens.")
    assert "- [ ] It works" in created["body"]

    _commit(worktree, "two.txt")
    (again,) = await ActivityEnvironment().run(activities.open_change_set, _input(run_id, worktree))
    assert again.number == PR_NUMBER
    assert again.head_sha == _git("rev-parse", f"refs/heads/{BRANCH}", cwd=remote)
    patched = [b for m, _p, b in fake.requests if m == "PATCH"]
    assert patched == [{"body": created["body"]}]
    assert sum(1 for m, p, _ in fake.requests if m == "POST" and p.endswith("/pulls")) == 1

    secrets = [TOKEN, auth_header(TOKEN).rpartition(" ")[2]]
    assert argvs
    assert not any(s in arg for s in secrets for argv in argvs for arg in argv)
    step_log = (tmp_path / "logs" / run_id / "open_pr" / "1" / "output.log").read_text(encoding="utf-8")
    assert "updated PR #12" in step_log
    assert not any(s in step_log for s in secrets)
    assert not any(s in _git("config", "--list", "--show-origin", cwd=worktree) for s in secrets)


async def test_refuses_default_branch(  # noqa: PLR0913, PLR0917
    settings: Settings, remote: Path, reporter: StepReporter, run_id: str, github: GitHubApp, fake: FakeGitHub
) -> None:
    worktree = await _prepare(settings, remote, reporter, run_id)
    _git("checkout", "-B", "main", cwd=worktree)
    _commit(worktree, "bad.txt")
    activities = ChangeSetActivities(github, reporter, remote_url=lambda _n: remote.as_uri())
    with pytest.raises(ApplicationError, match="default branch") as info:
        await ActivityEnvironment().run(activities.open_change_set, _input(run_id, worktree, branch="main"))
    assert info.value.non_retryable
    assert _git("rev-parse", "refs/heads/main", cwd=remote) != _git("rev-parse", "HEAD", cwd=worktree)
    assert not any(m == "POST" and p.endswith("/pulls") for m, p, _ in fake.requests)


async def test_without_github_app_is_non_retryable(
    settings: Settings, remote: Path, reporter: StepReporter, run_id: str
) -> None:
    worktree = await _prepare(settings, remote, reporter, run_id)
    assert _git("config", "user.name", cwd=worktree) == settings.commit_author_name
    activities = ChangeSetActivities(None, reporter)
    with pytest.raises(ApplicationError, match="not configured") as info:
        await ActivityEnvironment().run(activities.open_change_set, _input(run_id, worktree))
    assert info.value.non_retryable


async def test_bot_lookup_failure_falls_back_to_settings(
    settings: Settings, remote: Path, reporter: StepReporter, run_id: str, private_key: str
) -> None:
    down = httpx.MockTransport(lambda _r: httpx.Response(503, json={"message": "unavailable"}))
    broken = GitHubApp(1, private_key, api_url="https://gh.test", transport=down)
    worktree = await _prepare(settings, remote, reporter, run_id, github=broken)
    assert _git("config", "user.name", cwd=worktree) == settings.commit_author_name


async def test_refuses_when_worktree_is_on_another_branch(  # noqa: PLR0913, PLR0917
    settings: Settings, remote: Path, reporter: StepReporter, run_id: str, github: GitHubApp, fake: FakeGitHub
) -> None:
    worktree = await _prepare(settings, remote, reporter, run_id)
    _git("checkout", "-b", "release/1.2", cwd=worktree)
    _commit(worktree, "sneaky.txt")
    activities = ChangeSetActivities(github, reporter, remote_url=lambda _n: remote.as_uri())
    with pytest.raises(ApplicationError, match="not the run's branch") as info:
        await ActivityEnvironment().run(activities.open_change_set, _input(run_id, worktree))
    assert info.value.non_retryable
    assert _git("for-each-ref", "refs/heads/release", cwd=remote) == ""
    assert fake.requests == []


async def test_rejected_pr_is_non_retryable(  # noqa: PLR0913, PLR0917
    settings: Settings, remote: Path, reporter: StepReporter, run_id: str, github: GitHubApp, fake: FakeGitHub
) -> None:
    worktree = await _prepare(settings, remote, reporter, run_id)
    fake.reject_create = True
    activities = ChangeSetActivities(github, reporter, remote_url=lambda _n: remote.as_uri())
    with pytest.raises(ApplicationError, match="No commits") as info:
        await ActivityEnvironment().run(activities.open_change_set, _input(run_id, worktree))
    assert info.value.non_retryable
