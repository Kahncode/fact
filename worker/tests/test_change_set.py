"""
`push_branch` then `open_change_set` on `LocalRunner`: push to a local bare remote, then open or reuse its PR.

The fake GitHub reads the branch's head from that remote, as GitHub would after the push.
"""

import subprocess
from pathlib import Path

import httpx
import pytest
from change_set_github import BOT_ID, FULL_NAME, PR_NUMBER, WRITE_TOKEN, FakeGitHub, private_key
from fact.core.activity_contract import PushBranchInput
from fact.core.github import GitHubApp
from fact.core.settings import Settings
from fact.core.types import RepoCheckout, Task, WorkspaceHandle, WorkspaceSpec
from fact.worker.activities.change_set import ChangeSetActivities, OpenChangeSetInput
from fact.worker.activities.push import PushActivities
from fact.worker.activities.workspace import WorkspaceActivities
from fact.worker.git_push import auth_header
from fact.worker.pr_body import CheckResult
from fact.worker.reporting import StepReporter
from fact.worker.runners import LocalRunner
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

BRANCH = "fact/task-m1-11"


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)  # noqa: S603, S607
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


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
def fake(remote: Path) -> FakeGitHub:
    def branch_sha(branch: str) -> str | None:
        result = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],  # noqa: S607
            cwd=remote,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip() or None

    return FakeGitHub(branch_sha)


@pytest.fixture(scope="module")
def key() -> str:
    return private_key()


@pytest.fixture
def github(fake: FakeGitHub, key: str) -> GitHubApp:
    return fake.app(key)


@pytest.fixture
def reporter(client: httpx.AsyncClient, tmp_path: Path) -> StepReporter:
    return StepReporter("http://test", client, log_dir=tmp_path / "logs")


@pytest.fixture
def runner(settings: Settings, remote: Path) -> LocalRunner:
    return LocalRunner(settings, remote_url=lambda _n: remote.as_uri())


async def _prepare(settings: Settings, remote: Path, reporter: StepReporter, run_id: str, **kw: object) -> Path:
    runner = LocalRunner(settings, remote_url=lambda _n: remote.as_uri(), **kw)
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


def _handle(run_id: str, worktree: Path) -> WorkspaceHandle:
    return WorkspaceHandle(
        run_id=run_id, root=str(worktree.parent), task_queue="sandbox", repos={FULL_NAME: str(worktree)}
    )


def _input(run_id: str, worktree: Path, branch: str = BRANCH) -> OpenChangeSetInput:
    task = Task(key="task:M1-11", id="M1-11", title="Open the PR", acceptance_criteria=["It works"], path="t.md")
    return OpenChangeSetInput(
        run_id=run_id,
        task=task,
        handle=_handle(run_id, worktree),
        branch=branch,
        title="Open the PR",
        summary="Pushes and opens.",
        check_results=[CheckResult(name="test", outcome="passed")],
        run_url="http://fact.test/runs/1",
    )


async def _push(push: PushActivities, run_id: str, worktree: Path, attempt: int = 1, branch: str = BRANCH) -> str:
    spec = PushBranchInput(
        run_id=run_id,
        handle=_handle(run_id, worktree),
        repo=FULL_NAME,
        branch=branch,
        default_branch="main",
        attempt=attempt,
    )
    return await ActivityEnvironment().run(push.push_branch, spec)


async def test_pushes_then_creates_then_reuses_pr(  # noqa: PLR0913, PLR0917
    settings: Settings,
    remote: Path,
    reporter: StepReporter,
    runner: LocalRunner,
    run_id: str,
    github: GitHubApp,
    fake: FakeGitHub,
    tmp_path: Path,
) -> None:
    worktree = await _prepare(settings, remote, reporter, run_id, github=github)
    assert _git("config", "user.name", cwd=worktree) == "fact[bot]"
    assert _git("config", "user.email", cwd=worktree) == f"{BOT_ID}+fact[bot]@users.noreply.github.com"
    _commit(worktree, "one.txt")
    push = PushActivities(runner, github, reporter)
    activities = ChangeSetActivities(github, reporter)

    pushed = await _push(push, run_id, worktree)
    (pr,) = await ActivityEnvironment().run(activities.open_change_set, _input(run_id, worktree))

    assert (pr.repo, pr.number, pr.url) == (FULL_NAME, PR_NUMBER, "https://github.com/acme/widget/pull/12")
    assert pr.head_sha == pushed == _git("rev-parse", f"refs/heads/{BRANCH}", cwd=remote)
    (created,) = fake.posted_prs()
    assert (created["head"], created["base"], created["title"]) == (BRANCH, "main", "Open the PR")
    assert created["body"].startswith("Pushes and opens.")
    assert "- [ ] It works" in created["body"]
    assert {"contents": "write"} in fake.permissions_asked()  # the push's token, and nothing more

    _commit(worktree, "two.txt")
    await _push(push, run_id, worktree, attempt=2)
    (again,) = await ActivityEnvironment().run(activities.open_change_set, _input(run_id, worktree))
    assert again.number == PR_NUMBER
    assert again.head_sha == _git("rev-parse", f"refs/heads/{BRANCH}", cwd=remote) != pr.head_sha
    patched = [b for m, _p, b in fake.requests if m == "PATCH"]
    assert patched == [{"body": created["body"]}]
    assert len(fake.posted_prs()) == 1

    secrets = [WRITE_TOKEN, auth_header(WRITE_TOKEN).rpartition(" ")[2]]
    for stage in ("push", "open_pr"):
        step_log = (tmp_path / "logs" / run_id / stage / "1" / "output.log").read_text(encoding="utf-8")
        assert not any(s in step_log for s in secrets)
    assert not any(s in _git("config", "--list", "--show-origin", cwd=worktree) for s in secrets)


@pytest.mark.parametrize(("checkout", "branch"), [("main", "main"), ("release/1.2", BRANCH)])
async def test_push_refuses_the_default_or_another_branch(  # noqa: PLR0913, PLR0917
    settings: Settings,
    remote: Path,
    reporter: StepReporter,
    runner: LocalRunner,
    run_id: str,
    github: GitHubApp,
    checkout: str,
    branch: str,
) -> None:
    worktree = await _prepare(settings, remote, reporter, run_id)
    _git("checkout", "-B", checkout, cwd=worktree)
    _commit(worktree, "bad.txt")

    with pytest.raises(ApplicationError) as info:
        await _push(PushActivities(runner, github, reporter), run_id, worktree, branch=branch)
    assert info.value.non_retryable
    assert _git("for-each-ref", "--format=%(refname)", cwd=remote) == "refs/heads/main"
    assert _git("rev-parse", "refs/heads/main", cwd=remote) != _git("rev-parse", "HEAD", cwd=worktree)


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
    settings: Settings, remote: Path, reporter: StepReporter, run_id: str, key: str
) -> None:
    down = httpx.MockTransport(lambda _r: httpx.Response(503, json={"message": "unavailable"}))
    broken = GitHubApp(1, key, api_url="https://gh.test", transport=down)
    worktree = await _prepare(settings, remote, reporter, run_id, github=broken)
    assert _git("config", "user.name", cwd=worktree) == settings.commit_author_name


@pytest.mark.parametrize("problem", ["never_pushed", "rejected"])
async def test_a_pr_github_cannot_open_is_non_retryable(  # noqa: PLR0913, PLR0917
    settings: Settings,
    remote: Path,
    reporter: StepReporter,
    runner: LocalRunner,
    run_id: str,
    github: GitHubApp,
    fake: FakeGitHub,
    problem: str,
) -> None:
    worktree = await _prepare(settings, remote, reporter, run_id)
    if problem == "rejected":
        await _push(PushActivities(runner, github, reporter), run_id, worktree)
        fake.reject_create = True
    with pytest.raises(ApplicationError) as info:
        await ActivityEnvironment().run(ChangeSetActivities(github, reporter).open_change_set, _input(run_id, worktree))
    assert info.value.non_retryable
    assert bool(fake.posted_prs()) == (problem == "rejected")  # a branch GitHub does not have gets no PR request


async def test_restart_after_a_stop_reuses_the_branch_and_the_pr(  # noqa: PLR0913, PLR0917
    settings: Settings,
    remote: Path,
    reporter: StepReporter,
    runner: LocalRunner,
    run_id: str,
    github: GitHubApp,
    fake: FakeGitHub,
    client: httpx.AsyncClient,
) -> None:
    push = PushActivities(runner, github, reporter)
    activities = ChangeSetActivities(github, reporter)
    stopped = await _prepare(settings, remote, reporter, run_id, github=github)
    _commit(stopped, "one.txt")
    await _push(push, run_id, stopped)
    (pr,) = await ActivityEnvironment().run(activities.open_change_set, _input(run_id, stopped))
    await runner.teardown(_handle(run_id, stopped))

    body = {"workflow_id": "wf-2", "project": "fact", "task_key": "task:M1-11", "workflow_type": "standard"}
    restart = (await client.post("/api/v1/runs", json=body)).json()["id"]
    worktree = await _prepare(settings, remote, reporter, restart, github=github)
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=worktree) == BRANCH  # free again, not a suffixed branch
    assert _git("rev-parse", "HEAD", cwd=worktree) == pr.head_sha  # starts from what the stopped run pushed
    _commit(worktree, "two.txt")
    await _push(push, restart, worktree)
    (again,) = await ActivityEnvironment().run(activities.open_change_set, _input(restart, worktree))
    assert (again.number, again.url) == (pr.number, pr.url)
    assert len(fake.posted_prs()) == 1
