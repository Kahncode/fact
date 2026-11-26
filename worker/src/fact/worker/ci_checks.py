"""Read CI for a commit through the GitHub REST API: check runs, commit statuses, and GitHub Actions job logs."""

import re
from dataclasses import dataclass
from typing import Any

import httpx

PAGE_SIZE = 100
MAX_PAGES = 10  # 1,000 check runs on one commit is far more than any real CI
ACTIONS_APP = "github-actions"  # a check run of this app has the id of its Actions job
PASSING = frozenset({"success", "neutral", "skipped"})
COMPLETED = "completed"
_TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ", re.MULTILINE)  # each Actions log line starts with one


@dataclass(frozen=True)
class CICheck:
    """One check run or commit status, normalized: `status` is `completed` once it has a `conclusion`."""

    name: str
    status: str
    conclusion: str | None
    url: str
    job_id: int | None = None  # GitHub Actions job, whose logs can be downloaded
    output: str = ""  # the check run's own output text, or a status's description

    @property
    def done(self) -> bool:
        return self.status == COMPLETED

    @property
    def failed(self) -> bool:
        return self.done and self.conclusion not in PASSING


def _check_run(run: dict[str, Any]) -> CICheck:
    app = (run.get("app") or {}).get("slug")
    out = run.get("output") or {}
    text = "\n\n".join(str(out[k]) for k in ("title", "summary", "text") if out.get(k))
    return CICheck(
        name=str(run.get("name", "")),
        status=str(run.get("status", "")),
        conclusion=run.get("conclusion"),
        url=str(run.get("html_url") or run.get("details_url") or ""),
        job_id=run.get("id") if app == ACTIONS_APP else None,
        output=text,
    )


def _status(status: dict[str, Any]) -> CICheck:
    state = str(status.get("state", ""))
    pending = state == "pending"
    return CICheck(
        name=str(status.get("context", "")),
        status="pending" if pending else COMPLETED,
        conclusion=None if pending else state,
        url=str(status.get("target_url") or ""),
        output=str(status.get("description") or ""),
    )


async def _check_runs(client: httpx.AsyncClient, full_name: str, sha: str) -> list[CICheck]:
    """Return the latest check run of each name on `sha` (GitHub's default `filter=latest`), every page."""
    runs: list[CICheck] = []
    for page in range(1, MAX_PAGES + 1):
        params = {"per_page": str(PAGE_SIZE), "page": str(page)}
        body = (await client.get(f"/repos/{full_name}/commits/{sha}/check-runs", params=params)).json()
        batch = body.get("check_runs") or []
        runs.extend(_check_run(r) for r in batch)
        if len(batch) < PAGE_SIZE or len(runs) >= int(body.get("total_count", 0)):
            break
    return runs


async def _statuses(client: httpx.AsyncClient, full_name: str, sha: str) -> list[CICheck]:
    """Return the latest commit status of each context on `sha` (the combined status)."""
    params = {"per_page": str(PAGE_SIZE)}
    body = (await client.get(f"/repos/{full_name}/commits/{sha}/status", params=params)).json()
    return [_status(s) for s in body.get("statuses") or []]


async def list_checks(client: httpx.AsyncClient, full_name: str, sha: str) -> list[CICheck]:
    """Every check run and commit status on `sha`."""
    return [*await _check_runs(client, full_name, sha), *await _statuses(client, full_name, sha)]


async def job_log(client: httpx.AsyncClient, full_name: str, job_id: int) -> str:
    """Download an Actions job's log (GitHub redirects to a signed URL), without the per-line timestamps."""
    response = await client.get(f"/repos/{full_name}/actions/jobs/{job_id}/logs")
    return _TIMESTAMP.sub("", response.text.lstrip("﻿"))
