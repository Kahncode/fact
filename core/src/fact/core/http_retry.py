"""
HTTP transports that retry while the API restarts: the clients' (worker, CLI) way to ride out an upgrade.

A request is retried with exponential backoff (`RetryPolicy`) on a connection error (`httpx.ConnectError`,
`httpx.ConnectTimeout`: the request never reached the API, so any method is safe to send again) and on 502, 503 and
504 (a proxy or the API saying it is not ready), until `RetryPolicy.budget_s` has passed since the first attempt; then
the last error is raised or the last response returned.

A status is retried only when the request body can be sent again (bytes or JSON, not a streamed upload; the caller of a
streamed request retries it with a fresh stream), and, unless `RetryPolicy.statuses_any_method`, only for an
idempotent method: the API also answers 502/503 after doing work (`POST /runs/start` records a `start_failed` run when
Temporal refuses), so sending such a POST again would repeat that work. The worker's calls are reports (steps,
artifacts, usage, notifications), which call neither Temporal nor GitHub, so it retries them whatever the method; the
CLI does not.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

RETRY_STATUSES = frozenset({502, 503, 504})
RETRY_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout)


@dataclass(frozen=True)
class RetryPolicy:
    budget_s: float  # retry until this long after the first attempt
    first_delay_s: float = 0.25
    max_delay_s: float = 5.0  # the wait doubles after each attempt up to this
    statuses_any_method: bool = True  # retry 502/503/504 for a POST or PATCH too, not only an idempotent method

    def next_delay(self, delay: float) -> float:
        return min(delay * 2, self.max_delay_s)


WORKER_POLICY = RetryPolicy(budget_s=60.0)  # an API restart (an upgrade) takes a few seconds; a slow one up to a minute
# a person is waiting: a few seconds, then "is `fact dev` running?"; a POST answered 502/503 is not sent again
CLI_POLICY = RetryPolicy(budget_s=5.0, statuses_any_method=False)
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})


def retryable(policy: RetryPolicy, request: httpx.Request, response: httpx.Response) -> bool:
    return (
        response.status_code in RETRY_STATUSES
        and isinstance(request.stream, httpx.ByteStream)
        and (policy.statuses_any_method or request.method in IDEMPOTENT_METHODS)
    )


class AsyncRetryTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        policy: RetryPolicy,
        inner: httpx.AsyncBaseTransport | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._policy, self._inner = policy, inner or httpx.AsyncHTTPTransport()
        self._sleep, self._clock = sleep, clock

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        deadline, delay = self._clock() + self._policy.budget_s, self._policy.first_delay_s
        while True:
            try:
                response = await self._inner.handle_async_request(request)
            except RETRY_ERRORS:
                if self._clock() >= deadline:
                    raise
            else:
                if not retryable(self._policy, request, response) or self._clock() >= deadline:
                    return response
                await response.aclose()
            await self._sleep(min(delay, max(deadline - self._clock(), 0.0)))
            delay = self._policy.next_delay(delay)

    async def aclose(self) -> None:
        await self._inner.aclose()


class RetryTransport(httpx.BaseTransport):
    """`AsyncRetryTransport` for a sync `httpx.Client`."""

    def __init__(
        self,
        policy: RetryPolicy,
        inner: httpx.BaseTransport | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._policy, self._inner = policy, inner or httpx.HTTPTransport()
        self._sleep, self._clock = sleep, clock

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        deadline, delay = self._clock() + self._policy.budget_s, self._policy.first_delay_s
        while True:
            try:
                response = self._inner.handle_request(request)
            except RETRY_ERRORS:
                if self._clock() >= deadline:
                    raise
            else:
                if not retryable(self._policy, request, response) or self._clock() >= deadline:
                    return response
                response.close()
            self._sleep(min(delay, max(deadline - self._clock(), 0.0)))
            delay = self._policy.next_delay(delay)

    def close(self) -> None:
        self._inner.close()
