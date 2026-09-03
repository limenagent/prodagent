"""PLAN_FIRST dispatch must respect the same write-serialization discipline
as the REACTIVE batch: readonly steps run concurrently, write steps one at a
time — two side-effecting tools unblocked together by the DAG must never
race, while readonly siblings still parallelize.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from prodagent.backends.file.checkpoint import FileCheckpointStore
from prodagent.backends.file.event_log import FileEventLog
from prodagent.kernel.scheduler import Scheduler
from prodagent.kernel.types import LLMResponse, SideEffectLevel, ToolMeta
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.tooling.base import FunctionTool
from prodagent.tooling.dispatcher import ToolDispatcher
from tests.runtime._preset import preset_plan

_INTERVALS: dict[str, tuple[float, float]] = {}


def _tool(name: str, *, readonly: bool) -> FunctionTool:
    async def fn(**_: Any) -> dict:
        start = time.monotonic()
        await asyncio.sleep(0.06)
        _INTERVALS[name] = (start, time.monotonic())
        return {"action": name}

    return FunctionTool(
        name=name,
        fn=fn,
        meta=ToolMeta(
            name=name,
            is_readonly=readonly,
            # MEDIUM, not HIGH: HIGH would trip the fail-closed approval gate
            # in a test with no approval handler.
            side_effect_level=SideEffectLevel.LOW if readonly else SideEffectLevel.MEDIUM,
        ),
        schema={
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}},
        },
    )


def _plan(steps: list[dict]) -> FakeLLMAdapter:
    return FakeLLMAdapter(
        responses=[LLMResponse(content=json.dumps({"steps": steps}), stop_reason="end_turn")]
    )


@pytest.mark.asyncio
async def test_write_steps_never_overlap_and_readonly_parallelizes(tmp_path):
    _INTERVALS.clear()

    # dispatcher.dispatch as the tool executor = production wiring (factory
    # passes exactly this), so intervals reflect real dispatch latency.
    dispatcher = ToolDispatcher(
        {
            t.name: t
            for t in (
                _tool("w1", readonly=False),
                _tool("w2", readonly=False),
                _tool("r1", readonly=True),
                _tool("r2", readonly=True),
            )
        }
    )

    started = time.monotonic()
    executor = Scheduler(
        initial_plan=preset_plan(
            [
                {"id": "s1", "action": "w1", "params": {}, "depends_on": []},
                {"id": "s2", "action": "w2", "params": {}, "depends_on": []},
                {"id": "s3", "action": "r1", "params": {}, "depends_on": []},
                {"id": "s4", "action": "r2", "params": {}, "depends_on": []},
            ]
        ),
        tools=dispatcher.dispatch,
        system="sys",
        initial_messages=[{"role": "user", "content": "do"}],
        event_log=FileEventLog(tmp_path / "events"),
        checkpoint_store=FileCheckpointStore(directory=tmp_path / "checkpoints"),
        dispatcher=dispatcher,
    )

    from prodagent.kernel.types import RunCompletedEvent

    terminal = None
    async for event in executor.stream("do", run_id="r-ser"):
        if isinstance(event, RunCompletedEvent):
            terminal = event

    assert terminal is not None
    assert set(_INTERVALS) == {"w1", "w2", "r1", "r2"}, _INTERVALS.keys()

    w1s, w1e = _INTERVALS["w1"]
    w2s, w2e = _INTERVALS["w2"]
    overlap = min(w1e, w2e) - max(w1s, w2s)
    assert overlap <= 0, f"write steps overlapped by {overlap * 1000:.0f}ms"

    r1s, _ = _INTERVALS["r1"]
    r2s, _ = _INTERVALS["r2"]
    assert abs(r1s - r2s) < 0.05, "readonly steps did not start concurrently"

    elapsed = time.monotonic() - started
    # Writes serial (0.12) + readonly concurrent (0.06) ≈ 0.18; all-serial would be 0.24.
    assert elapsed < 0.22, f"readonly phase did not parallelize (elapsed {elapsed:.2f}s)"
