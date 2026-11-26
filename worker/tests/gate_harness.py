"""
Driving a running workflow from a test: wait for a state through the status query, answer gates through the update.

Every wait is bounded and fails as soon as the workflow closes, so a run that ends early or never gets there fails
the test with its last status instead of polling forever.
"""

import asyncio
from collections.abc import Callable, Iterable

from fact.core.gates import ANSWER_GATE, STATUS, GateAnswer, GateDecision, OpenGate, WorkflowStatus
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowHandle

POLL_S = 0.05
WAIT_S = 30.0
CLOSED = {
    EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED,
    EventType.EVENT_TYPE_WORKFLOW_EXECUTION_FAILED,
    EventType.EVENT_TYPE_WORKFLOW_EXECUTION_TIMED_OUT,
    EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CANCELED,
    EventType.EVENT_TYPE_WORKFLOW_EXECUTION_TERMINATED,
    EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW,
}


async def _last_event(handle: WorkflowHandle) -> EventType.ValueType:
    history = await handle.fetch_history()
    return history.events[-1].event_type


async def until_status(
    handle: WorkflowHandle, predicate: Callable[[WorkflowStatus], bool], *, idle: bool = False
) -> WorkflowStatus:
    """
    Poll the status query until `predicate` holds; return that status.

    With `idle`, also wait until the workflow is blocked with nothing in flight (its last event is a completed
    workflow task: no activity, timer or workflow task pending), so every activity it ran before has been called.
    """
    status: WorkflowStatus | None = None
    try:
        async with asyncio.timeout(WAIT_S):
            while True:
                status = await handle.query(STATUS, result_type=WorkflowStatus)
                last = await _last_event(handle)
                if last in CLOSED:
                    msg = f"workflow closed ({EventType.Name(last)}) while waiting; last status {status}"
                    raise AssertionError(msg)
                if predicate(status) and (not idle or last == EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED):
                    return status
                await asyncio.sleep(POLL_S)
    except TimeoutError:
        msg = f"no matching status after {WAIT_S} s; last status {status}"
        raise AssertionError(msg) from None


async def open_gate(handle: WorkflowHandle, after: Iterable[str] = ()) -> OpenGate:
    """Wait until a gate other than those in `after` is open and the workflow waits on it; return the gate."""
    seen = set(after)
    status = await until_status(handle, lambda s: s.gate is not None and s.gate.gate_id not in seen, idle=True)
    assert status.gate is not None
    return status.gate


async def answer(handle: WorkflowHandle, gate_id: str, decision: GateDecision, note: str = "") -> None:
    await handle.execute_update(ANSWER_GATE, GateAnswer(gate_id=gate_id, decision=decision, note=note))
