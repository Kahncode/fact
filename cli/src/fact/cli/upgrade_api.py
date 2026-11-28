"""
The API calls a breaking upgrade makes (`fact.cli.breaking`): the scheduler switch, the open runs, stop and start.

Each call is one attempt: the supervisor's loop must not wait on an API that restarts, so a failure raises
`UpgradeApiError` and the upgrade tries again at its next check.
"""

from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from fact.cli import projects
from fact.core.enums import RunStatus

API = "/api/v1"
HTTP_CONFLICT = 409
OPEN_STATUSES = (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING)


class UpgradeApiError(RuntimeError):
    """The API could not be reached or refused a call; try again later."""


@dataclass(frozen=True)
class OpenRun:
    run_id: str
    task_key: str
    project: str


class UpgradeApi(Protocol):
    def scheduler_on(self) -> bool: ...
    def set_scheduler(self, *, on: bool) -> None: ...
    def open_runs(self, project_names: list[str]) -> list[OpenRun]: ...
    def stop(self, run_id: str, reason: str) -> None: ...
    def finished(self, run_id: str) -> bool: ...
    def start(self, task_key: str, project: str) -> str | None: ...


class HttpUpgradeApi:
    """`UpgradeApi` over HTTP, through the CLI's client (`projects.make_client`) without its retries."""

    def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:  # noqa: ANN401
        try:
            with projects.make_client(None) as client:
                return client.request(method, f"{API}{path}", **kwargs)
        except httpx.HTTPError as exc:
            msg = f"cannot reach the API ({exc})"
            raise UpgradeApiError(msg) from exc

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:  # noqa: ANN401
        response = self._call(method, path, **kwargs)
        if response.is_error:
            msg = f"{method} {path} answered {response.status_code}: {response.text[:300]}"
            raise UpgradeApiError(msg)
        return response.json()

    def scheduler_on(self) -> bool:
        return bool(self._json("GET", "/scheduler")["on"])

    def set_scheduler(self, *, on: bool) -> None:
        self._json("POST", "/scheduler/on" if on else "/scheduler/off")

    def open_runs(self, project_names: list[str]) -> list[OpenRun]:
        found: list[OpenRun] = []
        for project in project_names:
            for status in OPEN_STATUSES:
                runs = self._json("GET", "/runs", params={"project": project, "status": status.value})
                found += [OpenRun(r["id"], r["task_key"], project) for r in runs]
        return found

    def stop(self, run_id: str, reason: str) -> None:
        """Ask the run to stop; one already ended (409) needs nothing more."""
        response = self._call("POST", f"/runs/{quote(run_id, safe='')}/stop", json={"reason": reason})
        if response.is_error and response.status_code != HTTP_CONFLICT:
            msg = f"stopping {run_id} answered {response.status_code}: {response.text[:300]}"
            raise UpgradeApiError(msg)

    def finished(self, run_id: str) -> bool:
        return self._json("GET", f"/runs/{quote(run_id, safe='')}")["status"] == RunStatus.FINISHED

    def start(self, task_key: str, project: str) -> str | None:
        """
        Start the task as a person would (`fact run --force-deps`); return why it was refused (409), or None.

        A refused start is not retried: the task already has a run (started before a crash), or was closed meanwhile.
        """
        body = {"task_key": task_key, "project": project, "override_deps": True}
        response = self._call("POST", "/runs/start", json=body)
        if response.status_code == HTTP_CONFLICT:
            return response.text[:300]
        if response.is_error:
            msg = f"starting {task_key} answered {response.status_code}: {response.text[:300]}"
            raise UpgradeApiError(msg)
        return None
