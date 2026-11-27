"""wait_for_ci against a mocked GitHub API, reporting to the real API app: pending then green, failures with logs."""

from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fact.core.activity_contract import WaitForCIInput
from fact.core.enums import StepOutcome
from fact.core.github import GitHubApp
from fact.core.types import PRRef, StepResult
from fact.worker.activities.ci import TAIL_BYTES, CIActivities
from fact.worker.reporting import StepReporter
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

REPO = "acme/widget"
SHA = "0123456789abcdef0123456789abcdef01234567"  # pragma: allowlist secret - a fake commit SHA
PR = PRRef(repo=REPO, number=7, url=f"https://github.com/{REPO}/pull/7", head_sha=SHA)
TOKEN = "ghs_ci_secret_42"  # noqa: S105 - fake
CHECK_RUNS = f"/repos/{REPO}/commits/{SHA}/check-runs"
STATUS = f"/repos/{REPO}/commits/{SHA}/status"
PULL = f"/repos/{REPO}/pulls/7"
JOB_LOGS = f"/repos/{REPO}/actions/jobs/{{}}/logs"
BLOB = "https://blob.test/logs/{}"
TRANSIENT = 2  # polls that answer 502 before GitHub recovers


def _run(name: str, status: str, conclusion: str | None = None, job_id: int = 1, app: str = "github-actions") -> dict:
    url = f"https://github.com/{REPO}/actions/runs/9/job/{job_id}"
    out = {"title": f"{name} failed", "summary": "see the log", "text": None}
    return {
        "id": job_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "html_url": url,
        "app": {"slug": app},
        "output": out,
    }


@dataclass
class FakeGitHub:
    """Answers each poll with the next scripted list of check runs (the last one repeats), plus fixed statuses."""

    polls: list[list[dict]]
    statuses: list[dict] = field(default_factory=list)
    logs: dict[int, str] = field(default_factory=dict)  # job id -> log; a missing job answers 410 (expired)
    fail_with: int | None = None
    transient: int = 0  # first check-run polls that answer 502
    mergeable_state: str = "clean"  # what the PR says once no check ran: `dirty` when it conflicts with its base
    pull_failures: int = 0  # first PR reads that answer 502
    pull_head: str = SHA  # the head the PR answers about: another SHA is a stale answer from before the push
    pull_reads: int = 0
    seen: list[str] = field(default_factory=list)

    async def handle(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911
        path = request.url.path
        if path == f"/repos/{REPO}/installation":
            return httpx.Response(200, json={"id": 7, "account": {"login": "acme"}})
        if path == "/app/installations/7/access_tokens":
            return httpx.Response(201, json={"token": TOKEN, "expires_at": "2099-01-01T00:00:00Z"})
        self.seen.append(f"{request.url.host}{path}")
        if self.fail_with:
            return httpx.Response(self.fail_with, json={"message": "nope"})
        if path == CHECK_RUNS and self.transient > 0:
            self.transient -= 1
            return httpx.Response(502, json={"message": "Bad Gateway"})
        if path == CHECK_RUNS:
            runs = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
            return httpx.Response(200, json={"total_count": len(runs), "check_runs": runs})
        if path in {PULL, STATUS}:
            return self._fixed(path)
        for job_id, text in self.logs.items():
            if path == JOB_LOGS.format(job_id):
                return httpx.Response(302, headers={"Location": BLOB.format(job_id)})
            if request.url.host == "blob.test" and path == f"/logs/{job_id}":
                assert "authorization" not in request.headers  # the signed URL needs no token, and must not get it
                return httpx.Response(200, text=text)
        return httpx.Response(410, json={"message": "Gone"})

    def _fixed(self, path: str) -> httpx.Response:
        """Answer the PR (read when no check ran) or the commit statuses, which never change during a test."""
        if path == PULL and self.pull_failures > 0:
            self.pull_failures -= 1
            return httpx.Response(502, json={"message": "Bad Gateway"})
        if path == PULL:
            self.pull_reads += 1
            body = {
                "mergeable": self.mergeable_state != "dirty",
                "mergeable_state": self.mergeable_state,
                "head": {"sha": self.pull_head},
            }
            return httpx.Response(200, json=body)
        return httpx.Response(200, json={"state": "pending", "statuses": self.statuses})


@pytest.fixture(scope="module")
def private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


@pytest.fixture
def reporter(client: httpx.AsyncClient, tmp_path: Path) -> StepReporter:
    return StepReporter("http://test", client, log_dir=tmp_path / "logs")


def _activities(fake: FakeGitHub, private_key: str, reporter: StepReporter) -> CIActivities:
    github = GitHubApp(1, private_key, api_url="https://gh.test", transport=httpx.MockTransport(fake.handle))
    return CIActivities(github, reporter, poll_interval_s=0)


async def _wait(activities: CIActivities, run_id: str, **kw: float) -> StepResult:
    spec = WaitForCIInput(run_id=run_id, stage_id="wait_for_ci", attempt=1, pr=PR, **kw)
    return await ActivityEnvironment().run(activities.wait_for_ci, spec)


async def test_pending_then_success(
    private_key: str, reporter: StepReporter, client: httpx.AsyncClient, run_id: str
) -> None:
    polls = [
        [_run("lint", "queued")],
        [_run("lint", "completed", "success"), _run("test", "in_progress", job_id=2)],
        [_run("lint", "completed", "success"), _run("test", "completed", "skipped", job_id=2)],
    ]
    statuses = [{"context": "ci/other", "state": "success", "target_url": "https://ci.test/1"}]
    fake = FakeGitHub(polls=list(polls), statuses=statuses)
    result = await _wait(_activities(fake, private_key, reporter), run_id)

    assert result.outcome == StepOutcome.PASSED
    assert result.summary == f"3 CI checks passed on {SHA[:12]}"
    assert fake.seen.count(f"gh.test{CHECK_RUNS}") == len(polls) + 1  # until all were complete twice in a row
    (step,) = (await client.get(f"/api/v1/runs/{run_id}")).json()["steps"]
    assert (step["stage_id"], step["kind"], step["outcome"]) == ("wait_for_ci", "github", "passed")


async def test_failure_downloads_the_job_log(
    private_key: str, reporter: StepReporter, client: httpx.AsyncClient, run_id: str
) -> None:
    log = "﻿" + "\n".join(f"2026-11-26T10:00:{i % 60:02d}.1234567Z line {i}" for i in range(2000)) + "\nE assert 1 == 2"
    runs = [
        _run("lint", "completed", "success"),
        _run("test (windows)", "completed", "failure", job_id=42),
        _run("sonar", "completed", "action_required", job_id=5, app="sonarcloud"),
    ]
    statuses = [{"context": "ci/deploy", "state": "error", "description": "deploy broke", "target_url": None}]
    fake = FakeGitHub(polls=[runs], statuses=statuses, logs={42: log})
    result = await _wait(_activities(fake, private_key, reporter), run_id)

    assert result.outcome == StepOutcome.FAILED_CHECKS
    assert result.summary.startswith(f"3 of 4 CI checks failed on {SHA[:12]}: test (windows) (failure), sonar")
    test, sonar, deploy = result.outputs["failures"]
    assert test["log_tail"].endswith("line 1999\nE assert 1 == 2")
    assert "2026-11-26T" not in test["log_tail"]  # Actions timestamps are stripped
    assert len(test["log_tail"].encode()) <= TAIL_BYTES
    assert test["url"] == f"https://github.com/{REPO}/actions/runs/9/job/42"
    stored = await client.get(f"/api/v1/artifacts/{test['log_ref']}")
    assert stored.status_code == httpx.codes.OK
    assert stored.text.startswith("line 0\n")  # the whole log, in the artifact store
    assert (sonar["log_tail"], "log_ref" in sonar) == ("sonar failed\n\nsee the log", False)  # not an Actions job
    assert (deploy["name"], deploy["conclusion"], deploy["log_tail"]) == ("ci/deploy", "error", "deploy broke")


async def test_expired_job_log_falls_back_to_the_check_output(
    private_key: str, reporter: StepReporter, run_id: str
) -> None:
    fake = FakeGitHub(polls=[[_run("test", "completed", "failure", job_id=3)]])
    result = await _wait(_activities(fake, private_key, reporter), run_id)

    (failure,) = result.outputs["failures"]
    assert failure["log_tail"] == "test failed\n\nsee the log"
    assert "log_ref" not in failure


async def test_ci_that_never_finishes_times_out(private_key: str, reporter: StepReporter, run_id: str) -> None:
    fake = FakeGitHub(polls=[[_run("test", "in_progress")]])
    result = await _wait(_activities(fake, private_key, reporter), run_id, timeout_s=0)

    assert result.outcome == StepOutcome.FAILED_CHECKS
    assert result.outputs == {"timed_out": True, "pending": ["test"]}


async def test_no_checks_at_all_pass_after_the_grace(private_key: str, reporter: StepReporter, run_id: str) -> None:
    fake = FakeGitHub(polls=[[]])
    result = await _wait(_activities(fake, private_key, reporter), run_id, no_checks_grace_s=0)

    assert result.outcome == StepOutcome.PASSED
    assert result.outputs == {"checks": 0}
    assert f"gh.test{PULL}" in fake.seen  # it made sure the PR does not conflict


async def test_a_conflicting_pr_is_a_conflict_at_once_not_green(
    private_key: str, reporter: StepReporter, run_id: str
) -> None:
    fake = FakeGitHub(polls=[[]], mergeable_state="dirty")
    result = await _wait(_activities(fake, private_key, reporter), run_id)  # the default 5 min grace

    assert result.outcome == StepOutcome.FAILED_CHECKS
    assert result.outputs == {"checks": 0, "conflict": True}
    assert "conflicts with its base branch" in result.summary
    assert fake.seen.count(f"gh.test{CHECK_RUNS}") == 1  # at the first poll, not after the grace


async def test_a_transient_error_reading_the_pr_only_skips_a_poll(
    private_key: str, reporter: StepReporter, run_id: str
) -> None:
    fake = FakeGitHub(polls=[[]], mergeable_state="dirty", pull_failures=1)
    result = await _wait(_activities(fake, private_key, reporter), run_id)

    assert result.outputs == {"checks": 0, "conflict": True}
    assert fake.seen.count(f"gh.test{CHECK_RUNS}") == 2  # noqa: PLR2004 - polled again after the failed read


async def test_app_not_installed_is_not_retried(private_key: str, reporter: StepReporter, run_id: str) -> None:
    fake = FakeGitHub(polls=[[]], fail_with=404)
    with pytest.raises(ApplicationError) as caught:
        await _wait(_activities(fake, private_key, reporter), run_id)
    assert caught.value.non_retryable
    assert TOKEN not in str(caught.value)


async def test_a_transient_error_only_skips_a_poll(private_key: str, reporter: StepReporter, run_id: str) -> None:
    fake = FakeGitHub(polls=[[_run("test", "completed", "success")]], transient=TRANSIENT)
    result = await _wait(_activities(fake, private_key, reporter), run_id)

    assert result.outcome == StepOutcome.PASSED
    assert fake.seen.count(f"gh.test{CHECK_RUNS}") == TRANSIENT + 2  # the failed polls, then complete twice


async def test_timeout_still_reports_failures_already_known(
    private_key: str, reporter: StepReporter, run_id: str
) -> None:
    runs = [_run("test", "completed", "failure", job_id=3), _run("deploy", "in_progress", job_id=4)]
    fake = FakeGitHub(polls=[runs])
    result = await _wait(_activities(fake, private_key, reporter), run_id, timeout_s=0)

    assert result.outcome == StepOutcome.FAILED_CHECKS
    assert "timed_out" not in result.outputs  # a fix session can still act on the known failure
    assert result.outputs["pending"] == ["deploy"]
    assert [f["name"] for f in result.outputs["failures"]] == ["test"]


async def test_a_stale_answer_about_the_previous_head_is_not_a_conflict(
    private_key: str, reporter: StepReporter, run_id: str
) -> None:
    fake = FakeGitHub(polls=[[]], mergeable_state="dirty", pull_head="0" * 40)  # GitHub has not seen the push yet
    result = await _wait(_activities(fake, private_key, reporter), run_id, no_checks_grace_s=0)

    assert result.outcome == StepOutcome.PASSED  # no conflict known for this head, and no CI: nothing to wait for
    assert fake.pull_reads == 1  # one read per poll: the poll is the retry
