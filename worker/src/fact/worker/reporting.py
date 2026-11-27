"""
Step reporter: the one way an activity reports a step to the factory API.

`StepReporter.step(...)` creates the step row, binds `run_id` and `step_id` to logs, keeps the step's output in a local
log file, and on exit uploads that file, patches the step with its outcome and usage, and returns a `StepResult`.

Steps never depend on the API being up: every API call is best effort (one retry), failures are logged, and the log
file stays on disk.
"""

import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fact.core.artifacts import step_key
from fact.core.claude_usage import ClaudeUsage
from fact.core.enums import StepKind, StepOutcome, StepStatus
from fact.core.logging import bind_run, get_logger
from fact.core.types import SUMMARY_MAX_BYTES, StepResult, Usage, limit_text

log = get_logger(__name__)

API_PREFIX = "/api/v1"
LOG_NAME = "output.log"
TRANSCRIPT_NAME = "transcript.jsonl"
EVENT_BATCH = 50
MAX_BUFFERED_EVENTS = 1000
UPLOAD_CHUNK = 64 * 1024
SERVER_ERROR = 500
RETRIES = 1  # one retry after the first failed call
DEFAULT_LOG_DIR = Path(tempfile.gettempdir()) / "fact-steps"


def _now() -> datetime:
    return datetime.now(UTC)


async def _file_chunks(path: Path) -> AsyncIterator[bytes]:
    with path.open("rb") as f:
        while chunk := f.read(UPLOAD_CHUNK):
            yield chunk


class StepContext:
    """What a step body sees: log lines, events, artifacts and the final result."""

    def __init__(self, reporter: StepReporter, run_id: str, stage_id: str, attempt: int, log_path: Path) -> None:
        self._reporter = reporter
        self.run_id = run_id
        self.stage_id = stage_id
        self.attempt = attempt
        self.step_id: str | None = None  # None when the API could not create the step
        self.log_path = log_path
        self._log_file = log_path.open("a", encoding="utf-8", newline="\n", buffering=1)
        self._events: list[dict[str, Any]] = []
        self.result: StepResult | None = None  # set by `finish`; final (with `log_ref`) after the block ends
        self.transcript_ref: str | None = None  # set by `attach_transcript`; kept even if the body raises
        # the usage reported so far (a whole session's, or a cut-short session's messages), recorded if the body
        # raises or never calls `finish`
        self.reported_usage: Usage | None = None

    def log(self, line: str) -> None:
        """Append a line to the step's local `output.log`."""
        self._log_file.write(line.rstrip("\n") + "\n")

    async def event(self, type_: str, payload: dict[str, Any] | None = None) -> None:
        """Buffer an event; a full batch is sent right away, the rest when the step ends."""
        if self.step_id is None:
            return  # the API never took the step: nowhere to send events
        self._events.append({"type": type_, "at": _now().isoformat(), "step_id": self.step_id, "payload": payload})
        if len(self._events) > MAX_BUFFERED_EVENTS:
            del self._events[: len(self._events) - MAX_BUFFERED_EVENTS]  # API is down: drop the oldest
        if len(self._events) % EVENT_BATCH == 0:  # after a failed flush, retry only at the next full batch
            await self.flush_events()

    async def flush_events(self) -> None:
        if not self._events or self.step_id is None:
            return
        batch, self._events = self._events, []
        sent = await self._reporter.post_events(self.run_id, batch)
        if not sent:
            self._events = batch + self._events  # kept for the final retry

    async def attach(self, name: str, path: Path) -> str:
        """Upload `path` as an artifact of this step and return its key (deterministic, even if the upload failed)."""
        key = step_key(self.run_id, self.stage_id, self.attempt, name)
        await self._reporter.upload(key, path)
        return key

    async def attach_transcript(self, path: Path) -> str | None:
        """Upload the agent transcript and record it as the step's `transcript_ref`; None if the upload failed."""
        key = step_key(self.run_id, self.stage_id, self.attempt, TRANSCRIPT_NAME)
        if self.step_id is not None and await self._reporter.upload(key, path):
            self.transcript_ref = key
        return self.transcript_ref

    def finish(
        self,
        outcome: StepOutcome,
        summary: str = "",
        outputs: dict[str, Any] | None = None,
        usage: Usage | None = None,
    ) -> None:
        """Record the step's outcome; it is sent when the `async with` block ends."""
        self.result = StepResult(
            outcome=outcome,
            summary=limit_text(summary, SUMMARY_MAX_BYTES),
            outputs=outputs or {},
            usage=usage or Usage(),
        )

    def close_log(self) -> None:
        self._log_file.close()


def _usage_fields(usage: Usage) -> dict[str, Any]:
    """Return the step's usage as reported; a cost nobody reported is sent as an explicit null, never 0."""
    return {
        "tokens_in": usage.tokens_in,
        "tokens_out": usage.tokens_out,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "cost_usd": None if usage.cost_usd is None else str(usage.cost_usd),
        "usage_partial": usage.partial,
        "turns": usage.turns,
        "peak_context_tokens": usage.peak_context_tokens,
    }


class StepReporter:
    def __init__(self, api_base_url: str, http_client: httpx.AsyncClient, log_dir: Path | None = None) -> None:
        self._base = api_base_url.rstrip("/") + API_PREFIX
        self._http = http_client
        self._log_dir = log_dir or DEFAULT_LOG_DIR

    @asynccontextmanager
    async def step(  # noqa: PLR0913, PLR0917
        self,
        run_id: str,
        stage_id: str,
        kind: StepKind,
        attempt: int,
        harness: str | None = None,
        model: str | None = None,
        *,
        name: str | None = None,
    ) -> AsyncIterator[StepContext]:
        """
        Report one step attempt.

        Raising in the body gives `failed_error` and re-raises; a body that never calls
        `finish` also ends as `failed_error`, since silence is not a pass.
        """
        step_key(run_id, stage_id, attempt, LOG_NAME)  # refuses unsafe ids before anything touches the disk
        log_path = self._log_dir / run_id / stage_id / str(attempt) / LOG_NAME
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.unlink(missing_ok=True)  # a fresh attempt never appends to an old file
        ctx = StepContext(self, run_id, stage_id, attempt, log_path)
        started = _now()
        ctx.step_id = await self._create_step(run_id, stage_id, kind, attempt, harness, model, started, name=name)
        try:
            with bind_run(run_id, ctx.step_id):
                try:
                    yield ctx
                except BaseException as exc:
                    error = limit_text(f"{type(exc).__name__}: {exc}", SUMMARY_MAX_BYTES)
                    ctx.log(f"step failed: {error}")
                    ctx.finish(StepOutcome.FAILED_ERROR, error, usage=ctx.reported_usage)
                    raise
                finally:
                    await self._finalize(ctx, started)
        finally:
            ctx.close_log()

    async def _finalize(self, ctx: StepContext, started: datetime) -> None:
        if ctx.result is None:
            ctx.log("step ended without reporting an outcome")
            ctx.finish(StepOutcome.FAILED_ERROR, "Step ended without reporting an outcome", usage=ctx.reported_usage)
        result = ctx.result
        ctx.close_log()
        await ctx.flush_events()
        log_ref = None
        if ctx.step_id is not None:
            key = step_key(ctx.run_id, ctx.stage_id, ctx.attempt, LOG_NAME)
            log_ref = key if await self.upload(key, ctx.log_path) else None
            await ctx.flush_events()  # the one retry for events that failed earlier
            await self._patch_step(ctx.step_id, result, log_ref, ctx.transcript_ref, started)
        ctx.result = result.model_copy(update={"log_ref": log_ref, "transcript_ref": ctx.transcript_ref})
        log.info("step_finished", outcome=result.outcome, log_ref=log_ref, stage_id=ctx.stage_id)

    async def _create_step(  # noqa: PLR0913, PLR0917
        self,
        run_id: str,
        stage_id: str,
        kind: StepKind,
        attempt: int,
        harness: str | None,
        model: str | None,
        started: datetime,
        *,
        name: str | None = None,
    ) -> str | None:
        body = {
            "stage_id": stage_id,
            "name": name,
            "kind": kind,
            "attempt": attempt,
            "queued_at": started.isoformat(),
            "started_at": started.isoformat(),
            "harness": harness,
            "model": model,
        }
        response = await self._send("create step", "POST", f"/runs/{run_id}/steps", json=body)
        try:
            return response.json()["id"] if response is not None else None
        except ValueError, KeyError, TypeError:
            log.warning("report_failed", what="create step", error="unexpected response body")
            return None

    async def _patch_step(
        self,
        step_id: str,
        result: StepResult,
        log_ref: str | None,
        transcript_ref: str | None,
        started: datetime,
    ) -> None:
        body: dict[str, Any] = {
            "status": StepStatus.FINISHED,
            "outcome": result.outcome,
            "started_at": started.isoformat(),
            "ended_at": _now().isoformat(),
            **_usage_fields(result.usage),
            "outputs": result.outputs,
        }
        if log_ref is not None:
            body["log_ref"] = log_ref
        if transcript_ref is not None:
            body["transcript_ref"] = transcript_ref
        await self._send("update step", "PATCH", f"/steps/{step_id}", json=body)

    async def post_events(self, run_id: str, events: list[dict[str, Any]]) -> bool:
        return await self._send("post events", "POST", f"/runs/{run_id}/events", json=events) is not None

    async def report_usage(self, account: str, usage: ClaudeUsage) -> bool:
        """Record the subscription usage windows a run saw (`PUT /usage/<account>`); False if the API refused."""
        windows = [w.model_dump(mode="json") for w in usage.windows]
        return await self._send("report usage", "PUT", f"/usage/{account}", json={"windows": windows}) is not None

    async def download_text(self, key: str) -> str | None:
        """Read a UTF-8 artifact; None (after one retry) if the API refused or was unreachable."""
        response = await self._send("download artifact", "GET", f"/artifacts/{key}")
        return response.content.decode("utf-8", errors="replace") if response is not None else None

    async def upload(self, key: str, path: Path) -> bool:
        """Upload a file; False (after one retry) if the API refused or was unreachable. The file stays on disk."""
        response = await self._send("upload artifact", "PUT", f"/artifacts/{key}", content=lambda: _file_chunks(path))
        return response is not None

    async def _send(self, what: str, method: str, path: str, **kwargs: Any) -> httpx.Response | None:  # noqa: ANN401
        """One API call with one retry. Never raises on API trouble: returns None and logs a warning."""
        for attempt in range(RETRIES + 1):
            args = {k: (v() if callable(v) else v) for k, v in kwargs.items()}
            try:
                response = await self._http.request(method, self._base + path, **args)
                response.raise_for_status()
            except (httpx.HTTPError, OSError) as exc:
                log.warning("report_failed", what=what, attempt=attempt + 1, error=str(exc) or type(exc).__name__)
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < SERVER_ERROR:
                    break  # a 4xx will not succeed on retry
            else:
                return response
        return None
