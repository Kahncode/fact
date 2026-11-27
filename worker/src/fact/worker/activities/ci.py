"""
`wait_for_ci`: wait for CI on a PR's head commit, as a `github` step, and report whether it passed.

Polls the commit's check runs and commit statuses every `poll_interval_s` until all are complete on two
polls in a row or `timeout_s` passes; a transient GitHub error only skips a poll. On a timeout, checks that already
failed are reported as failures, else the step says `timed_out`. A failed GitHub Actions job's log goes to the artifact store; its last `TAIL_BYTES` go in the
step's `outputs["failures"]` (and as much as fits in its summary), for the fix session. While no check has
appeared, each poll also reads the PR: one that conflicts with its base (`mergeable_state` `dirty`) gets no
`pull_request` workflow from GitHub, so the step fails at once with `outputs.conflict`; no check at all after
`no_checks_grace_s` on a PR that does not conflict passes.
"""

import asyncio
import time
from typing import Any

import httpx
from fact.core.activity_contract import WAIT_FOR_CI, WaitForCIInput
from fact.core.enums import StepKind, StepOutcome
from fact.core.github import GitHubApp, GitHubError
from fact.core.types import SUMMARY_MAX_BYTES, PRRef, StepResult, limit_text
from fact.worker.ci_checks import CICheck, job_log, list_checks
from fact.worker.heartbeats import previous_details, report
from fact.worker.pull_requests import merge_state
from fact.worker.reporting import StepContext, StepReporter
from temporalio import activity
from temporalio.exceptions import ApplicationError

POLL_INTERVAL_S = 30.0
TAIL_BYTES = 4096  # end of each failed job's log handed to the fix session
MAX_LOGS = 10  # failed jobs whose logs are downloaded; the rest are named only
PERMANENT_STATUSES = frozenset({401, 404, 422})  # bad credentials, commit or App not found, bad request


def _non_retryable(message: str) -> ApplicationError:
    return ApplicationError(message, non_retryable=True)


def _tail(text: str) -> str:
    return text.encode("utf-8")[-TAIL_BYTES:].decode("utf-8", errors="ignore")


def _started_at() -> float:
    """Wall-clock start of the wait, reported to the heartbeats so a retried attempt keeps the same deadline."""
    details = previous_details()
    started = float(details[0]) if details and isinstance(details[0], int | float) else time.time()
    report(started)
    return started


def _summary(failures: list[dict[str, Any]], total: int, sha: str) -> str:
    names = ", ".join(f"{f['name']} ({f['conclusion']})" for f in failures)
    text = f"{len(failures)} of {total} CI checks failed on {sha[:12]}: {names}"
    for f in failures:
        text += f"\n\n{f['name']}:\n{f['log_tail']}"
    return limit_text(text, SUMMARY_MAX_BYTES)


class CIActivities:
    """Control zone: reads CI with the GitHub App and reports the wait as a step."""

    def __init__(
        self, github: GitHubApp | None, reporter: StepReporter, poll_interval_s: float = POLL_INTERVAL_S
    ) -> None:
        self._github = github
        self._reporter = reporter
        self._poll_s = poll_interval_s

    @activity.defn(name=WAIT_FOR_CI)
    async def wait_for_ci(self, spec: WaitForCIInput) -> StepResult:
        async with self._reporter.step(spec.run_id, spec.stage_id, StepKind.GITHUB, spec.attempt) as step:
            try:
                await self._wait(spec, step)
            except GitHubError as exc:
                if exc.status_code in PERMANENT_STATUSES:
                    raise _non_retryable(str(exc)) from exc
                raise
        if step.result is None:
            msg = "step ended without a result"
            raise RuntimeError(msg)
        return step.result

    async def _wait(self, spec: WaitForCIInput, step: StepContext) -> None:
        github = self._github
        if github is None:
            msg = "GitHub App not configured: set FACT_GITHUB_APP_ID and FACT_GITHUB_APP_PRIVATE_KEY_PATH"
            raise _non_retryable(msg)
        pr, started = spec.pr, _started_at()
        step.log(f"waiting for CI on {pr.repo}@{pr.head_sha} ({pr.url})")
        checks: list[CICheck] = []
        done_before = False  # every check was complete on the previous poll too
        while True:
            polled = await self._poll(github, pr, step)
            waited = time.time() - started
            if polled is not None:
                checks = polled
                done = bool(checks) and all(c.done for c in checks)
                if done and done_before:  # twice in a row: suites that start late (`needs:`, other workflows) had time
                    await self._finish(github, spec, step, checks)
                    return
                done_before = done
                if not checks and await self._no_checks(github, spec, step, waited):
                    return
            if waited >= spec.timeout_s:
                await self._finish(github, spec, step, checks, waited=waited)
                return
            step.log(f"{sum(not c.done for c in checks)} of {len(checks)} checks pending after {waited:.0f}s")
            await asyncio.sleep(self._poll_s)

    @staticmethod
    async def _no_checks(github: GitHubApp, spec: WaitForCIInput, step: StepContext, waited: float) -> bool:
        """
        No check yet: end the step if the PR conflicts with its base, or if no CI came within the grace; else False.

        GitHub starts no `pull_request` workflow on a PR that conflicts with its base (`mergeable_state` `dirty`), so it
        is read on every poll without checks: a conflict ends `failed_checks` with `outputs.conflict` at once, and the
        workflow merges the base instead of waiting for a CI that never comes. Past `no_checks_grace_s` without a conflict
        (any other state, or none known for this head yet), the repository has no CI: `passed`. A transient error
        reading the PR only skips the poll.
        """
        pr = spec.pr
        try:
            token = await github.installation_token(pr.repo)
            async with github.api_client(token.token) as client:
                state = await merge_state(client, pr.repo, pr.number, pr.head_sha, tries=1)  # the poll retries
        except GitHubError as exc:
            if exc.status_code in PERMANENT_STATUSES:
                raise
            step.log(f"reading the PR failed, polling again: {exc}")
            return False
        except httpx.HTTPError as exc:
            step.log(f"reading the PR failed, polling again: {type(exc).__name__}: {exc}")
            return False
        if state == "dirty":
            summary = f"{pr.url} conflicts with its base branch, so no CI runs on {pr.head_sha[:12]}"
            step.log(summary)
            step.finish(StepOutcome.FAILED_CHECKS, summary, {"checks": 0, "conflict": True})
            return True
        if waited < spec.no_checks_grace_s:
            return False
        step.log(f"no CI check or status on the commit (mergeable_state {state}): nothing to wait for")
        step.finish(StepOutcome.PASSED, f"no CI checks on {pr.head_sha[:12]}", {"checks": 0})
        return True

    @staticmethod
    async def _poll(github: GitHubApp, pr: PRRef, step: StepContext) -> list[CICheck] | None:
        """List the checks; None after a transient error, so one bad poll in an hour's wait does not end it."""
        try:
            token = await github.installation_token(pr.repo)  # cached until near expiry
            async with github.api_client(token.token) as client:
                return await list_checks(client, pr.repo, pr.head_sha)
        except GitHubError as exc:
            if exc.status_code in PERMANENT_STATUSES:
                raise
            step.log(f"polling CI failed, trying again: {exc}")
        except httpx.HTTPError as exc:
            step.log(f"polling CI failed, trying again: {type(exc).__name__}: {exc}")
        return None

    async def _finish(
        self,
        github: GitHubApp,
        spec: WaitForCIInput,
        step: StepContext,
        checks: list[CICheck],
        *,
        waited: float = 0,
    ) -> None:
        """Report the result; after `waited` seconds (a timeout), failures already known still go to a fix."""
        failed = [c for c in checks if c.failed]
        pending = [c.name for c in checks if not c.done]
        for c in checks:
            step.log(f"{c.name}: {c.conclusion or c.status}")
        if pending and not failed:
            summary = f"CI still running after {waited / 60:.0f} min: {', '.join(pending)}"
            step.log(summary)
            step.finish(StepOutcome.FAILED_CHECKS, summary, {"timed_out": True, "pending": pending})
            return
        if not failed:
            step.finish(StepOutcome.PASSED, f"{len(checks)} CI checks passed on {spec.pr.head_sha[:12]}")
            return
        token = await github.installation_token(spec.pr.repo)
        async with github.api_client(token.token) as client:
            failures = []
            for i, check in enumerate(failed):
                failures.append(await self._failure(client, spec, step, check, fetch=i < MAX_LOGS))
        outputs = {"head_sha": spec.pr.head_sha, "checks": len(checks), "failures": failures, "pending": pending}
        step.finish(StepOutcome.FAILED_CHECKS, _summary(failures, len(checks), spec.pr.head_sha), outputs)

    async def _failure(
        self, client: httpx.AsyncClient, spec: WaitForCIInput, step: StepContext, check: CICheck, *, fetch: bool
    ) -> dict[str, Any]:
        """Describe a failed check for the fix session: its job log's tail (whole as an artifact), else its output."""
        failure: dict[str, Any] = {"name": check.name, "conclusion": check.conclusion, "url": check.url}
        text = check.output
        if fetch and check.job_id is not None:
            try:
                text = await job_log(client, spec.pr.repo, check.job_id)
            except (GitHubError, httpx.HTTPError) as exc:  # logs expired or not reachable: the output will do
                step.log(f"{check.name}: no job log ({exc})")
            else:
                path = step.log_path.parent / f"job-{check.job_id}.log"
                path.write_text(text, encoding="utf-8", newline="\n")
                failure["log_ref"] = await step.attach(path.name, path)
        failure["log_tail"] = _tail(text)
        return failure
