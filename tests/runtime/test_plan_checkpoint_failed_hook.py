from __future__ import annotations

import json

import pytest

from prodagent.backends.file.checkpoint import FileCheckpointStore
from prodagent.backends.file.event_log import FileEventLog
from prodagent.kernel.bus import HookEvent, HookRegistry
from prodagent.kernel.types import LLMResponse
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.plan.executor import PlanExecutor


class _RecordingExecutor:
    async def __call__(self, call) -> dict:
        return {"status": "green", "action": call.name}


def _two_step_plan_llm() -> FakeLLMAdapter:
    plan = {
        "steps": [
            {"id": "s1", "action": "collect", "params": {}, "depends_on": []},
            {"id": "s2", "action": "report", "params": {}, "depends_on": ["s1"]},
        ]
    }
    return FakeLLMAdapter(responses=[LLMResponse(content=json.dumps(plan), stop_reason="end_turn")])


@pytest.mark.asyncio
async def test_plan_checkpoint_failure_fires_once_via_hooks(tmp_path, monkeypatch):
    import prodagent.backends.file.checkpoint as checkpoint_module

    def _boom(*_a, **_k):
        raise OSError("simulated disk full")

    monkeypatch.setattr(checkpoint_module, "write_atomic_json", _boom)

    events = FileEventLog(tmp_path / "events")
    checkpoints = FileCheckpointStore(directory=tmp_path / "checkpoints")

    hooks = HookRegistry()
    seen: list[dict] = []
    hooks.register_event(HookEvent.CHECKPOINT_FAILED, lambda **kw: seen.append(kw))

    planner = PlanExecutor(
        _two_step_plan_llm(),
        _RecordingExecutor(),
        system="sys",
        messages=[{"role": "user", "content": "do"}],
        event_log=events,
        checkpoint_store=checkpoints,
        hooks=hooks,
    )

    final_run = None
    from prodagent.kernel.events import RunCompletedEvent

    async for event in planner.stream("do the thing", run_id="R1"):
        if isinstance(event, RunCompletedEvent):
            final_run = event.run

    assert final_run is not None
    assert final_run.checkpoint_failed is True, "checkpoint write kept failing throughout the run"
    assert len(seen) == 1, f"CHECKPOINT_FAILED must fire exactly once, fired {len(seen)} times"
    assert seen[0]["run_id"] == "R1"


@pytest.mark.asyncio
async def test_plan_checkpoint_success_never_fires(tmp_path):
    events = FileEventLog(tmp_path / "events")
    checkpoints = FileCheckpointStore(directory=tmp_path / "checkpoints")

    hooks = HookRegistry()
    seen: list[dict] = []
    hooks.register_event(HookEvent.CHECKPOINT_FAILED, lambda **kw: seen.append(kw))

    planner = PlanExecutor(
        _two_step_plan_llm(),
        _RecordingExecutor(),
        system="sys",
        messages=[{"role": "user", "content": "do"}],
        event_log=events,
        checkpoint_store=checkpoints,
        hooks=hooks,
    )

    async for _ in planner.stream("do the thing", run_id="R2"):
        pass

    assert seen == []
