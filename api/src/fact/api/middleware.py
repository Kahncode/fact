"""Request-id and request-log middleware, and the JSON exception handler."""

import asyncio
import re
import time
import uuid

from fact.api.body_capture import BodyCapture, mask_headers, mask_query
from fact.core.logging import get_logger
from fact.core.settings import AppEnv
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from structlog.contextvars import bound_contextvars

log = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
_UNLOGGED_PATHS = frozenset({"/health"})


def _request_id(headers: Headers) -> str:
    """Return the caller's `X-Request-ID` if it is short and plain, else a new id (the value ends up in logs)."""
    supplied = headers.get(REQUEST_ID_HEADER)
    return supplied if supplied and _VALID_REQUEST_ID.fullmatch(supplied) else str(uuid.uuid4())


class RequestIdMiddleware:
    """
    Pure ASGI middleware: binds `request_id` in structlog, sets the response header and logs one line per request.

    It wraps the whole app, so the id is also present on the 500 responses made by the exception handler. The log line
    has method, path, query and headers (secret-looking ones masked), status and latency. JSON bodies up to 8 KiB are included
    with secret-looking keys masked; larger or non-JSON bodies are left out.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        request_id = _request_id(headers)
        scope.setdefault("state", {})["request_id"] = request_id
        status = 500  # what the client gets if the app raises before sending a response
        started = False
        request_body = BodyCapture(headers)
        response_body = BodyCapture(Headers())

        async def receive_capturing() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                request_body.add(message.get("body", b""))
            return message

        async def send_with_id(message: Message) -> None:
            nonlocal status, response_body, started
            if message["type"] == "http.response.start":
                started = True
                status = message["status"]
                response_body = BodyCapture(Headers(raw=message["headers"]))
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            elif message["type"] == "http.response.body":
                response_body.add(message.get("body", b""))
            await send(message)

        start = time.perf_counter()
        with bound_contextvars(request_id=request_id):
            try:
                await self.app(scope, receive_capturing, send_with_id)
            except asyncio.CancelledError:
                if not started:
                    status = 499  # the client went away before a response began
                raise
            finally:
                self._log_request(scope, headers, status, start, (request_body, response_body))

    @staticmethod
    def _log_request(
        scope: Scope, headers: Headers, status: int, start: float, bodies: tuple[BodyCapture, BodyCapture]
    ) -> None:
        if scope["path"] in _UNLOGGED_PATHS:
            return
        try:
            RequestIdMiddleware._emit(scope, headers, status, start, bodies)
        except Exception:
            log.warning("request_log_failed", exc_info=True)  # logging must never fail a request

    @staticmethod
    def _emit(
        scope: Scope, headers: Headers, status: int, start: float, bodies: tuple[BodyCapture, BodyCapture]
    ) -> None:
        request_body, response_body = bodies
        log.info(
            "request_completed",
            http_request={
                "method": scope["method"],
                "url": scope["path"],
                "query": mask_query(scope["query_string"]),
                "headers": mask_headers(headers),
                "body": request_body.value(),
            },
            http_response={
                "status_code": status,
                "processing_time": int((time.perf_counter() - start) * 1000),
                "body": response_body.value(),
            },
        )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = request.scope.get("state", {}).get("request_id")
    # ServerErrorMiddleware sits outside RequestIdMiddleware: the contextvar is already unbound, so pass the id.
    log.error("unhandled_exception", path=request.url.path, request_id=request_id, exc_info=exc)
    settings = getattr(request.app.state, "settings", None)
    content = {"error": "internal_server_error", "request_id": request_id}
    if settings is not None and settings.env in (AppEnv.LOCAL, AppEnv.TEST):
        content["detail"] = f"{type(exc).__name__}: {exc}"
    return JSONResponse(
        status_code=500,
        content=content,
        headers={REQUEST_ID_HEADER: request_id} if request_id else None,
    )
