"""The API's Temporal client: connected on first use, so the API starts even while Temporal is down."""

import asyncio

from fact.core.settings import Settings
from fastapi import HTTPException, Request, status
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter


class TemporalConnection:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Client | None = None
        self._lock = asyncio.Lock()

    @property
    def address(self) -> str:
        return self._settings.temporal.address

    async def client(self) -> Client:
        async with self._lock:
            if self._client is None:
                self._client = await Client.connect(
                    self._settings.temporal.address,
                    namespace=self._settings.temporal.namespace,
                    data_converter=pydantic_data_converter,
                )
            return self._client


async def temporal_client(request: Request) -> Client:
    """Return the app's client (tests replace `app.state.temporal`); 503 while Temporal cannot be reached."""
    return await connect_temporal(request.app.state.temporal)


async def connect_temporal(connection: TemporalConnection) -> Client:
    """Return the connection's client; 503 while Temporal cannot be reached."""
    try:
        return await connection.client()
    except (RuntimeError, OSError) as exc:
        address = connection.address
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Cannot reach Temporal at {address}: {exc}") from exc
