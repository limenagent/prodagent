from __future__ import annotations

import json

from prodagent import Agent, AgentConfig
from prodagent.coordination.parent_runtime import ParentRuntime
from prodagent.coordination.spawn import Spawn
from prodagent.core.budget import HardBudget
from prodagent.core.types import LLMResponse
from prodagent.llm.fake import FakeLLMAdapter


def _plan_llm() -> FakeLLMAdapter:
    plan = {"steps": [{"id": "s1", "action": "noop", "params": {}, "depends_on": []}]}
    return FakeLLMAdapter(responses=[LLMResponse(content=json.dumps(plan), stop_reason="end_turn")])


async def test_spawned_child_trips_on_spend_already_committed_by_a_sibling():
    budget = HardBudget(max_turns=50, max_cost_usd=0.9, max_tokens=1_000_000, max_seconds=600)
    ctx = ParentRuntime(budget=budget)
    ctx.accumulator.cost_usd = 0.95
    ctx.accumulator.spawn_count = 1

    child = Agent(
        "worker",
        system_prompt="do work",
        config=AgentConfig(name="worker", llm=_plan_llm(), description="A PLAN_FIRST worker"),
    )
    pipeline = Spawn([child], llm=_plan_llm(), hooks=None, framework_config=None, ctx=ctx)

    result = await pipeline.spawn("worker", "do something")

    assert result["state"] == "failed", result
    assert "cost" in result["output"].lower() or "Cost limit" in result["output"]


async def test_spawned_child_completes_when_sibling_spend_stays_under_ceiling():
    budget = HardBudget(max_turns=50, max_cost_usd=0.9, max_tokens=1_000_000, max_seconds=600)
    ctx = ParentRuntime(budget=budget)
    ctx.accumulator.cost_usd = 0.1
    ctx.accumulator.spawn_count = 1

    child = Agent(
        "worker",
        system_prompt="do work",
        config=AgentConfig(name="worker", llm=_plan_llm(), description="A PLAN_FIRST worker"),
    )
    pipeline = Spawn([child], llm=_plan_llm(), hooks=None, framework_config=None, ctx=ctx)

    result = await pipeline.spawn("worker", "do something")

    assert result["state"] != "failed", result
