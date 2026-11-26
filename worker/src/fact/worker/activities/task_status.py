"""
`report_status`: tell the task's source about a run event (labels, comments, PR links, closing).

The source is the one the project's `.fact/project.yaml` names, read from the project's local checkout. Reporting is
bookkeeping: a project without a checkout, or whose source cannot be written yet, is skipped with a log line.
"""

import asyncio

from fact.core.activity_contract import REPORT_STATUS, ReportStatusInput
from fact.core.github import GitHubAppConfigError, GitHubError
from fact.core.logging import bind_run, get_logger
from fact.core.project_config import ConfigError, load_project_config
from fact.core.settings import Settings, get_settings, project_path
from fact.core.tasks import TaskSource, source_for
from fact.core.tasks.events import report
from temporalio import activity
from temporalio.exceptions import ApplicationError

log = get_logger(__name__)


def _permanent(exc: GitHubError) -> bool:
    """Tell whether a retry would fail the same way: a 4xx other than 429 (a 403 rate limit is not retried either)."""
    code = exc.status_code or 0
    return 400 <= code < 500 and code != 429  # noqa: PLR2004 - HTTP status classes


class TaskStatusActivities:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings

    async def _source(self, project: str) -> TaskSource | None:
        settings = self._settings or get_settings()
        ref = next((r for r in settings.projects if r.name == project), None)
        path = project_path(ref) if ref else None
        if path is None:
            return None
        try:
            config = await asyncio.to_thread(load_project_config, path)
        except ConfigError as exc:
            raise ApplicationError(str(exc), non_retryable=True) from exc
        try:
            return source_for(config, settings, path)
        except GitHubAppConfigError as exc:  # reporting is bookkeeping: skip it rather than retry forever
            log.warning("task_source_unavailable", project=project, error=str(exc))
            return None

    @activity.defn(name=REPORT_STATUS)
    async def report_status(self, spec: ReportStatusInput) -> None:
        with bind_run(spec.run_id):
            source = await self._source(spec.project)
            if source is None:
                log.info("report_status_skipped", project=spec.project, task_key=spec.task_key, task_event=spec.event)
                return
            try:
                await report(source, spec)
            except GitHubError as exc:
                if _permanent(exc):  # no access, no such issue, refused: retrying only delays the run
                    raise ApplicationError(str(exc), non_retryable=True) from exc
                raise
            log.info("task_status_reported", task_key=spec.task_key, task_event=spec.event)
