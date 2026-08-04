from __future__ import annotations

import asyncio
import json

import pytest

from prodagent.backends.file.checkpoint import FileCheckpointStore
from prodagent.backends.file.event_log import FileEventLog
from prodagent.core.event_log import PlanEventType
from prodagent.core.types import LLMResponse
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.runtime.executors.plan_first import PlanExecutor


def _plan_llm(*plans: dict) -> FakeLLMAdapter:
    return FakeLLMAdapter(
        responses=[LLMResponse(content=json.dumps(p), stop_reason="end_turn") for p in plans]
    )


def _parallel_plan() -> dict:
    return {
        "steps": [
            {"id": "s1", "action": "prep", "params": {}, "depends_on": []},
            {"id": "s2", "action": "work_a", "params": {}, "depends_on": ["s1"]},
            {"id": "s3", "action": "work_b", "params": {}, "depends_on": ["s1"]},
        ]
    }


def _stores(tmp_path):
    return (
        FileEventLog(tmp_path / "events"),
        FileCheckpointStore(directory=tmp_path / "checkpoints"),
    )


class _CancellableExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.work_b_started = asyncio.Event()

    async def __call__(self, call) -> dict:
        self.calls.append(call.name)
        if call.name == "work_b":
            self.work_b_started.set()
            await asyncio.sleep(60)
        return {"status": "ok", "action": call.name}


@pytest.mark.asyncio
async def test_cancel_after_one_step_completes_persists_event(tmp_path):
    events, checkpoints = _stores(tmp_path)
    executor = _CancellableExecutor()
    planner = PlanExecutor(
        _plan_llm(_parallel_plan()),
        executor,
        system="sys",
        messages=[{"role": "user", "content": "do"}],
        event_log=events,
        checkpoint_store=checkpoints,
    )

    run_id = "R-cancel-1"
    task = asyncio.create_task(_drain_stream(planner, run_id))
    await executor.work_b_started.wait()
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises((asyncio.CancelledError, Exception)):
        await task

    logged = await events.get_events(run_id)
    step_completed = [
        e
        for e in logged
        if e.event_type is PlanEventType.STEP_COMPLETED and e.data.get("step_id") == "s2"
    ]
    assert step_completed, "work_a (s2) completed before cancel — StepCompleted must be persisted"


async def _drain_stream(planner: PlanExecutor, run_id: str) -> None:
    async for _ in planner.stream("do", run_id=run_id):
        pass
