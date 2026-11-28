"""The clients' retrying transports: they ride out an API restart, then give up after their budget."""

import httpx
import pytest
from fact.core.http_retry import AsyncRetryTransport, RetryPolicy, RetryTransport

POLICY = RetryPolicy(budget_s=60.0)


class Clock:
    """A fake monotonic clock that the transports' `sleep` advances."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    async def async_sleep(self, seconds: float) -> None:
        self.sleep(seconds)


class Restarting:
    """An API down until `back_at` on the clock: refusing connections, then answering 503, then up."""

    def __init__(self, clock: Clock, back_at: float) -> None:
        self.clock, self.back_at, self.calls = clock, back_at, 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.clock.now < self.back_at / 2:
            msg = "refused"
            raise httpx.ConnectError(msg, request=request)
        if self.clock.now < self.back_at:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})


async def test_async_retries_until_the_api_is_back() -> None:
    clock = Clock()
    api = Restarting(clock, back_at=30.0)
    transport = AsyncRetryTransport(POLICY, httpx.MockTransport(api), sleep=clock.async_sleep, clock=clock)
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.post("http://api/x", json={"a": 1})
    assert response.json() == {"ok": True}
    assert max(clock.slept) == POLICY.max_delay_s  # backoff doubles up to its cap


def test_sync_gives_up_after_its_budget() -> None:
    clock = Clock()
    api = Restarting(clock, back_at=1000.0)
    transport = RetryTransport(RetryPolicy(budget_s=5.0), httpx.MockTransport(api), sleep=clock.sleep, clock=clock)
    with httpx.Client(transport=transport) as client, pytest.raises(httpx.ConnectError):
        client.get("http://api/x")
    assert clock.now == pytest.approx(5.0)  # retried for the whole budget, not a moment less


@pytest.mark.parametrize("status", [404, 500])
def test_other_answers_are_not_retried(status: int) -> None:
    clock = Clock()
    transport = RetryTransport(POLICY, httpx.MockTransport(lambda _: httpx.Response(status)), sleep=clock.sleep)
    with httpx.Client(transport=transport) as client:
        assert client.get("http://api/x").status_code == status
    assert clock.slept == []
