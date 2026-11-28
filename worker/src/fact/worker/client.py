"""The worker's clients: Temporal, and the HTTP client every call to the factory API goes through."""

import httpx
from fact.core.http_retry import WORKER_POLICY, AsyncRetryTransport
from fact.core.settings import Settings
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter


async def connect(settings: Settings) -> Client:
    """Connect using the address and namespace from settings, with the Pydantic data converter."""
    return await Client.connect(
        settings.temporal.address,
        namespace=settings.temporal.namespace,
        data_converter=pydantic_data_converter,
    )


def api_http(timeout_s: float) -> httpx.AsyncClient:
    """HTTP client for the factory API: retries connection errors and 502/503/504 while the API restarts."""
    return httpx.AsyncClient(timeout=timeout_s, transport=AsyncRetryTransport(WORKER_POLICY))
