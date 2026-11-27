from pathlib import Path

import httpx
import pytest
from fact.core.artifacts import InvalidArtifactKeyError
from fact.core.enums import StepKind, StepOutcome
from fact.core.types import Usage
from fact.worker.reporting import StepReporter
from httpx import AsyncClient

BASE = "http://test"
COST = 0.5
TOKENS_IN = 3
TOKENS_OUT = 4


def _reporter(client: AsyncClient, tmp_path: Path) -> StepReporter:
    return StepReporter(BASE, client, log_dir=tmp_path / "logs")


async def _steps(client: AsyncClient, run_id: str) -> list[dict]:
    return (await client.get(f"/api/v1/runs/{run_id}")).json()["steps"]


async def test_passing_step(client: AsyncClient, run_id: str, tmp_path: Path) -> None:
    async with _reporter(client, tmp_path).step(run_id, "implement", StepKind.SHELL, 1, harness="h") as s:
        s.log("first")
        s.log("second")
        await s.event("log", {"n": 1})
        s.finish(StepOutcome.PASSED, "ok", {"k": "v"}, Usage(tokens_in=TOKENS_IN, tokens_out=TOKENS_OUT, cost_usd=COST))
    assert s.result is not None
    assert s.result.outcome == StepOutcome.PASSED
    assert s.result.log_ref == f"runs/{run_id}/implement/1/output.log"

    (step,) = await _steps(client, run_id)
    assert step["status"] == "finished"
    assert step["outcome"] == "passed"
    assert step["harness"] == "h"
    assert step["started_at"] is not None
    assert step["ended_at"] is not None
    assert step["tokens_in"] == TOKENS_IN
    assert step["tokens_out"] == TOKENS_OUT
    assert float(step["cost_usd"]) == COST
    assert step["outputs"] == {"k": "v"}
    assert step["log_ref"] == s.result.log_ref
    got = await client.get(f"/api/v1/artifacts/{step['log_ref']}")
    assert got.text == "first\nsecond\n"


async def test_raising_step_is_failed_error_and_reraised(client: AsyncClient, run_id: str, tmp_path: Path) -> None:
    async def failing() -> None:
        async with _reporter(client, tmp_path).step(run_id, "tests", StepKind.SHELL, 1) as s:
            s.log("before")
            msg = "boom"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="boom"):
        await failing()
    (step,) = await _steps(client, run_id)
    assert step["outcome"] == "failed_error"
    assert step["status"] == "finished"
    log = (await client.get(f"/api/v1/artifacts/{step['log_ref']}")).text
    assert "before" in log
    assert "RuntimeError: boom" in log


async def test_no_finish_is_failed_error(client: AsyncClient, run_id: str, tmp_path: Path) -> None:
    async with _reporter(client, tmp_path).step(run_id, "x", StepKind.SHELL, 1):
        pass
    (step,) = await _steps(client, run_id)
    assert step["outcome"] == "failed_error"
    assert (step["turns"], step["peak_context_tokens"]) == (None, None)  # no agent session


async def test_attach_uploads_artifact(client: AsyncClient, run_id: str, tmp_path: Path) -> None:
    diff = tmp_path / "d.patch"
    diff.write_text("patch", encoding="utf-8")
    async with _reporter(client, tmp_path).step(run_id, "implement", StepKind.SHELL, 2) as s:
        key = await s.attach("diff.patch", diff)
        s.finish(StepOutcome.PASSED)
    assert key == f"runs/{run_id}/implement/2/diff.patch"
    assert (await client.get(f"/api/v1/artifacts/{key}")).text == "patch"


async def test_events_are_flushed(client: AsyncClient, run_id: str, tmp_path: Path, app) -> None:  # noqa: ANN001
    async with _reporter(client, tmp_path).step(run_id, "implement", StepKind.SHELL, 1) as s:
        for i in range(60):  # one full batch of 50, the rest on exit
            await s.event("log", {"i": i})
        s.finish(StepOutcome.PASSED)
    from fact.core.models import Event  # noqa: PLC0415
    from sqlalchemy import func, select  # noqa: PLC0415

    async with app.state.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == 60  # noqa: PLR2004


async def test_api_unreachable_does_not_fail_step(tmp_path: Path) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        msg = "down"
        raise httpx.ConnectError(msg, request=request)

    async with AsyncClient(transport=httpx.MockTransport(refuse)) as down:
        reporter = StepReporter(BASE, down, log_dir=tmp_path / "logs")
        async with reporter.step("r1", "implement", StepKind.SHELL, 1) as s:
            s.log("kept")
            await s.event("log")
            s.finish(StepOutcome.PASSED, "ok")
    assert s.step_id is None
    assert s.result is not None
    assert s.result.outcome == StepOutcome.PASSED
    assert s.result.log_ref is None
    assert s.log_path.read_text(encoding="utf-8") == "kept\n"


async def test_upload_retried_once(client: AsyncClient, run_id: str, tmp_path: Path) -> None:
    calls = {"put": 0}
    real = client._transport  # noqa: SLF001

    class Flaky(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.method == "PUT":
                calls["put"] += 1
                if calls["put"] == 1:
                    return httpx.Response(503)
            return await real.handle_async_request(request)

    async with (
        AsyncClient(transport=Flaky(), base_url=BASE) as flaky,
        _reporter(flaky, tmp_path).step(run_id, "implement", StepKind.SHELL, 1) as s,
    ):
        s.log("x")
        s.finish(StepOutcome.PASSED)
    assert calls["put"] == 2  # noqa: PLR2004
    assert s.result is not None
    assert s.result.log_ref is not None
    assert (await client.get(f"/api/v1/artifacts/{s.result.log_ref}")).text == "x\n"


async def test_unsafe_stage_id_refused_before_disk(client: AsyncClient, run_id: str, tmp_path: Path) -> None:
    async def run() -> None:
        async with _reporter(client, tmp_path).step(run_id, "../evil", StepKind.SHELL, 1):
            pass

    with pytest.raises(InvalidArtifactKeyError):
        await run()
    assert not (tmp_path / "logs").exists()
