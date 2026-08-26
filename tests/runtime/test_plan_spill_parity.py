"""PLAN_FIRST tool results must go through the same spill throat as REACTIVE.

Both execution modes share one dispatcher and one ``build_tool_message`` —
a plan step returning a huge payload gets truncated to a ``<spilled>``
placeholder exactly like a REACTIVE batch would, instead of accumulating
raw in the transcript the replanner then pays for.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from prodagent.backends.file.checkpoint import FileCheckpointStore
from prodagent.backends.file.event_log import FileEventLog
from prodagent.base.config import ContextConfig
from prodagent.cognition.context.budget import TokenCounter
from prodagent.cognition.context.spill import ToolResultSpillStore
from prodagent.kernel.types import LLMResponse, RunCompletedEvent, SideEffectLevel, ToolMeta
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.plan.executor import PlanExecutor
from prodagent.tooling.base import FunctionTool
from prodagent.tooling.dispatcher import ToolDispatcher


def _plan_llm() -> FakeLLMAdapter:
    plan = {"steps": [{"id": "s1", "action": "big_query", "params": {}, "depends_on": []}]}
    return FakeLLMAdapter(responses=[LLMResponse(content=json.dumps(plan), stop_reason="end_turn")])


def _big_tool() -> FunctionTool:
    async def fn(**_: Any) -> dict:
        return {"rows": "x" * 50_000}

    return FunctionTool(
        name="big_query",
        fn=fn,
        meta=ToolMeta(
            name="big_query",
            is_readonly=True,
            side_effect_level=SideEffectLevel.LOW,
            max_result_chars=200,
        ),
        schema={
            "name": "big_query",
            "description": "returns a lot",
            "parameters": {"type": "object", "properties": {}},
        },
    )


@pytest.mark.asyncio
async def test_plan_step_tool_result_is_spill_truncated(tmp_path):
    store = ToolResultSpillStore(tmp_path / "spill", counter=TokenCounter())
    dispatcher = ToolDispatcher({"big_query": _big_tool()})
    dispatcher.configure_batch(context_config=ContextConfig(), spill_store=store)

    executor = PlanExecutor(
        _plan_llm(),
        dispatcher.dispatch,
        system="sys",
        messages=[{"role": "user", "content": "do"}],
        event_log=FileEventLog(tmp_path / "events"),
        checkpoint_store=FileCheckpointStore(directory=tmp_path / "checkpoints"),
        dispatcher=dispatcher,
    )

    terminal = None
    async for event in executor.stream("do", run_id="r-spill"):
        if isinstance(event, RunCompletedEvent):
            terminal = event

    assert terminal is not None
    tool_msgs = [m for m in terminal.run.messages if m.get("role") == "tool"]
    assert tool_msgs, "plan step produced no tool message"
    assert "<spilled" in tool_msgs[-1]["content"]
    assert len(tool_msgs[-1]["content"]) < 5_000  # placeholder + preview, not the raw 50k
    assert store.spill_count == 1
