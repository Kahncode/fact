"""Capture small JSON bodies for the request log, with secrets masked."""

import json
from typing import Any
from urllib.parse import parse_qsl, urlencode

from starlette.datastructures import Headers

MAX_LOGGED_BODY_BYTES = 8 * 1024
_SECRET_WORDS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "authorization",
        "credential",
        "credentials",
        "signature",
        "cookie",
        "apikey",
    }
)
_MASK = "***"


def is_secret_key(key: str) -> bool:
    """Tell whether a JSON key, header name or query parameter holds a secret (`access_token`, `X-API-Key`, ...)."""
    normalized = key.lower().replace("-", "_")
    return (
        any(word in _SECRET_WORDS for word in normalized.split("_"))
        or "api_key" in normalized
        or "private_key" in normalized
    )


def mask_headers(headers: Headers) -> dict[str, str]:
    return {k: _MASK if is_secret_key(k) else v for k, v in headers.items()}


def mask_query(query_string: bytes) -> str:
    """Return the query string with the value of every secret-looking parameter masked."""
    pairs = parse_qsl(query_string.decode("latin-1"), keep_blank_values=True)
    return urlencode([(k, _MASK if is_secret_key(k) else v) for k, v in pairs], safe="*")


def is_json(headers: Headers) -> bool:
    return headers.get("content-type", "").split(";")[0].strip().lower() == "application/json"


def mask_secrets(value: Any) -> Any:  # noqa: ANN401
    """Return a copy of parsed JSON with the value of every secret-looking key replaced by `***`."""
    if isinstance(value, dict):
        return {k: _MASK if is_secret_key(str(k)) else mask_secrets(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_secrets(v) for v in value]
    return value


class BodyCapture:
    """Collects body chunks up to a limit; `value()` is the masked JSON, or None if empty, too big or not JSON."""

    def __init__(self, headers: Headers, limit: int = MAX_LOGGED_BODY_BYTES) -> None:
        declared = headers.get("content-length")
        self.enabled = is_json(headers) and not (declared and declared.isdigit() and int(declared) > limit)
        self.limit = limit
        self.size = 0
        self.chunks: list[bytes] = []

    def add(self, chunk: bytes) -> None:
        if not self.enabled:
            return
        self.size += len(chunk)
        if self.size > self.limit:
            self.enabled = False
            self.chunks.clear()
        else:
            self.chunks.append(chunk)

    def value(self) -> Any:  # noqa: ANN401
        if not self.enabled or not self.chunks:
            return None
        try:
            return mask_secrets(json.loads(b"".join(self.chunks)))
        except ValueError, RecursionError:  # deeply nested JSON must never break logging
            return None
