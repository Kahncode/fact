import uuid

from fact.worker.ping import CONTROL_QUEUE, PingWorkflow, ping
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker


async def test_ping_workflow() -> None:
    env_cm = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    async with env_cm as env, Worker(env.client, task_queue=CONTROL_QUEUE, workflows=[PingWorkflow], activities=[ping]):
        result = await env.client.execute_workflow(
            PingWorkflow.run, id=f"ping-{uuid.uuid4()}", task_queue=CONTROL_QUEUE
        )
    assert result == "pong"
