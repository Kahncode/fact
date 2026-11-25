from http import HTTPStatus

from fact.api.body_capture import BodyCapture, mask_secrets
from fact.api.projects import sync_projects
from fact.core.models import Repository
from fact.core.settings import AppEnv, Settings
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select
from starlette.datastructures import Headers
from structlog.contextvars import merge_contextvars
from structlog.testing import capture_logs

PROJECT_COUNT = 2
UUID_LENGTH = 36


async def test_health(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Request-ID"]


async def test_projects_lists_fact_with_repository(client: AsyncClient) -> None:
    response = await client.get("/api/v1/projects")
    assert response.status_code == HTTPStatus.OK
    by_name = {p["name"]: p for p in response.json()}
    assert by_name["fact"]["home_repo"] == "Kahncode/fact"
    assert [r["full_name"] for r in by_name["fact"]["repositories"]] == ["Kahncode/fact"]
    assert by_name["fact"]["repositories"][0]["role"] == "factory"
    assert by_name["remote"]["repositories"] == []


async def test_sync_is_idempotent(app: FastAPI, client: AsyncClient, settings: Settings) -> None:
    await sync_projects(settings, app.state.sessions)
    await sync_projects(settings, app.state.sessions)
    async with app.state.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Repository)) == 1
    assert len((await client.get("/api/v1/projects")).json()) == PROJECT_COUNT


async def test_unhandled_error_returns_json(app: FastAPI, client: AsyncClient) -> None:
    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError

    response = await client.get("/boom")
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    body = response.json()
    assert body["error"] == "internal_server_error"
    assert body["request_id"]
    assert response.headers["X-Request-ID"] == body["request_id"]


async def test_supplied_request_id_is_echoed_and_bad_one_replaced(client: AsyncClient) -> None:
    ok = await client.get("/api/v1/projects", headers={"X-Request-ID": "abc-123"})
    assert ok.headers["X-Request-ID"] == "abc-123"
    bad = await client.get("/api/v1/projects", headers={"X-Request-ID": "x" * 500})
    assert bad.headers["X-Request-ID"] != "x" * 500
    assert len(bad.headers["X-Request-ID"]) == UUID_LENGTH


async def test_request_log_has_request_id_and_no_credentials(client: AsyncClient) -> None:
    with capture_logs(processors=[merge_contextvars]) as logs:
        await client.get("/api/v1/projects?x=1", headers={"Authorization": "Bearer secret", "X-Request-ID": "rid-1"})
    (entry,) = [e for e in logs if e["event"] == "request_completed"]
    assert entry["request_id"] == "rid-1"
    assert entry["http_request"]["url"] == "/api/v1/projects"
    assert entry["http_request"]["query"] == "x=1"
    assert entry["http_request"]["headers"]["authorization"] == "***"
    assert entry["http_response"]["status_code"] == HTTPStatus.OK


async def test_health_is_not_logged(client: AsyncClient) -> None:
    with capture_logs(processors=[merge_contextvars]) as logs:
        await client.get("/health")
    assert not [e for e in logs if e["event"] == "request_completed"]


async def test_unhandled_error_log_carries_request_id(app: FastAPI, client: AsyncClient) -> None:
    @app.get("/boom2")
    async def boom() -> None:
        raise RuntimeError

    with capture_logs(processors=[merge_contextvars]) as logs:
        await client.get("/boom2", headers={"X-Request-ID": "rid-2"})
    (entry,) = [e for e in logs if e["event"] == "unhandled_exception"]
    assert entry["request_id"] == "rid-2"


async def test_small_json_bodies_are_logged_with_secrets_masked(client: AsyncClient) -> None:
    with capture_logs(processors=[merge_contextvars]) as logs:
        await client.post("/api/v1/runs", json={"project": "nope", "api_token": "s3cret"})
    (entry,) = [e for e in logs if e["event"] == "request_completed"]
    assert entry["http_request"]["body"] == {"project": "nope", "api_token": "***"}
    assert entry["http_response"]["body"]


async def test_large_body_is_not_logged(client: AsyncClient) -> None:
    with capture_logs(processors=[merge_contextvars]) as logs:
        await client.post("/api/v1/runs", json={"project": "x" * 20_000})
    (entry,) = [e for e in logs if e["event"] == "request_completed"]
    assert entry["http_request"]["body"] is None


async def test_headers_and_query_secrets_are_masked(client: AsyncClient) -> None:
    with capture_logs(processors=[merge_contextvars]) as logs:
        await client.get("/api/v1/projects?token=abc&x=1", headers={"X-API-Key": "k", "X-Hub-Signature": "s"})
    (entry,) = [e for e in logs if e["event"] == "request_completed"]
    assert entry["http_request"]["query"] == "token=***&x=1"
    assert entry["http_request"]["headers"]["x-api-key"] == "***"
    assert entry["http_request"]["headers"]["x-hub-signature"] == "***"


def test_mask_secrets_keeps_token_counts_and_survives_deep_nesting() -> None:
    assert mask_secrets({"access_token": "t", "input_tokens": 5, "nested": [{"password": "p"}]}) == {
        "access_token": "***",
        "input_tokens": 5,
        "nested": [{"password": "***"}],
    }
    deep = BodyCapture(Headers({"content-type": "application/json"}))
    deep.add(b"[" * 5000 + b"]" * 5000)
    assert deep.value() is None


async def test_unhandled_error_detail_only_in_local_and_test(
    app: FastAPI, client: AsyncClient, settings: Settings
) -> None:
    @app.get("/boom3")
    async def boom() -> None:
        raise RuntimeError("secret-dsn")  # noqa: EM101

    assert "detail" in (await client.get("/boom3")).json()
    app.state.settings = settings.model_copy(update={"env": AppEnv.PROD})
    body = (await client.get("/boom3")).json()
    assert "detail" not in body
    assert "secret-dsn" not in str(body)
