"""Temporal client factory shared by the worker and anything else that talks to Temporal."""

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
