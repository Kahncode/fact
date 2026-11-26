"""Driving gates from a test: wait for one to open through the status query, and answer it through the update."""

import asyncio
from collections.abc import Iterable

from fact.core.gates import ANSWER_GATE, STATUS, GateAnswer, GateDecision, OpenGate, WorkflowStatus
from temporalio.client import WorkflowHandle

POLL_S = 0.05


async def open_gate(handle: WorkflowHandle, after: Iterable[str] = ()) -> OpenGate:
    """Poll the status query until a gate other than those in `after` is open; return it."""
    seen = set(after)
    while True:
        status = await handle.query(STATUS, result_type=WorkflowStatus)
        if status.gate is not None and status.gate.gate_id not in seen:
            return status.gate
        await asyncio.sleep(POLL_S)


async def answer(handle: WorkflowHandle, gate_id: str, decision: GateDecision, note: str = "") -> None:
    await handle.execute_update(ANSWER_GATE, GateAnswer(gate_id=gate_id, decision=decision, note=note))
