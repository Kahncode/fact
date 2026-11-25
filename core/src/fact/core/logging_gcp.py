"""Processor that reshapes a JSON log event for Google Cloud Logging."""

from structlog.typing import EventDict, WrappedLogger

_SEVERITY = {
    "debug": "DEBUG",
    "info": "INFO",
    "warning": "WARNING",
    "error": "ERROR",
    "critical": "CRITICAL",
}


def format_gcloud_logs(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """
    Rename `event` to `message` and `level` to `severity`.

    A request log carries `http_request` (method, url, query, headers, body) and optionally `http_response`
    (status_code, processing_time in ms, body). They become the `httpRequest` object Cloud Logging shows in its request
    view. Bodies, query and headers are kept as top-level `request_body`, `request_query`, `request_headers` and
    `response_body`, because `httpRequest` only accepts its own fields.
    """
    if "event" in event_dict:
        event_dict["message"] = event_dict.pop("event")
    event_dict["severity"] = _SEVERITY.get(str(event_dict.pop("level", "")).lower(), "DEFAULT")

    request = event_dict.pop("http_request", None)
    if not isinstance(request, dict):
        return event_dict
    response = event_dict.pop("http_response", None) or {}
    headers = request.get("headers") or {}
    http: dict[str, object] = {"requestMethod": request.get("method"), "requestUrl": request.get("url")}
    for source, key, target in (
        (request, "body", "request_body"),
        (request, "query", "request_query"),
        (request, "headers", "request_headers"),
        (response, "body", "response_body"),
    ):
        if source.get(key):
            event_dict[target] = source[key]
    if "status_code" in response:
        http["status"] = response["status_code"]
    if "processing_time" in response:
        http["latency"] = f"{response['processing_time'] / 1000}s"
    if "user-agent" in headers:
        http["userAgent"] = headers["user-agent"]
    if "content-length" in headers:
        http["requestSize"] = headers["content-length"]
    event_dict["httpRequest"] = http
    return event_dict
