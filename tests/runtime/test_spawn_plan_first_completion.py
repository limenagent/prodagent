from __future__ import annotations

import json

import pytest

from prodagent.core.types import ExecutionMode, LLMResponse
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.runtime.agent import Agent
from prodagent.tooling import tool


def _plan_llm() -> FakeLLMAdapter:
    plan = {
        "steps": [
            {"id": "s1", "action": "collect", "params": {}, "depends_on": []},
            {"id": "s2", "action": "report", "params": {}, "depends_on": ["s1"], "terminal": True},
        ]
    }
    return FakeLLMAdapter(responses=[LLMResponse(content=json.dumps(plan), stop_reason="end_turn")])


@pytest.mark.asyncio
async def test_plan_first_child_reports_completed_not_failed():

    @tool(name="collect", readonly=True)
    async def collect() -> dict:
        return {"data": "ok"}

    @tool(name="report", readonly=True)
    async def report() -> dict:
        return {"summary": "all good"}

    child = Agent(
        "worker", context="do the work", tools=[collect, report], llm=_plan_llm()
    ).description("A PLAN_FIRST worker")
    assert child.mode is ExecutionMode.PLAN_FIRST

    from prodagent.runtime.coordination.fork import ParentRuntime
    from prodagent.runtime.coordination.spawn import build_spawn_tools_for_agent

    spawn = build_spawn_tools_for_agent([child], llm=_plan_llm(), context=ParentRuntime())
    result = await spawn.tool._fn(name="worker", task="collect and report")

    assert result["state"] != "failed", (
        f"PLAN_FIRST child succeeded but spawn reported failed: {result}"
    )
    assert result["state"] == "completed", f"expected completed, got {result['state']}"
    assert "all good" in result["output"] or "summary" in result["output"]
