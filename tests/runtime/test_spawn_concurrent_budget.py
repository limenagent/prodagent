from __future__ import annotations

import json

from prodagent import Agent, AgentConfig
from prodagent.runtime.parent_runtime import ParentRuntime
from prodagent.coordination.spawn import Spawn
from prodagent.kernel.budget import HardBudget
from prodagent.kernel.types import LLMResponse
from prodagent.llm.fake import FakeLLMAdapter


def _plan_llm() -> FakeLLMAdapter:
    plan = {"steps": [{"id": "s1", "action": "noop", "params": {}, "depends_on": []}]}
    return FakeLLMAdapter(responses=[LLMResponse(content=json.dumps(plan), stop_reason="end_turn")])


async def test_spawned_child_trips_on_spend_already_committed_by_a_sibling():
    from prodagent.kernel.budget import BudgetLedger

    budget = HardBudget(max_turns=50, max_cost_usd=0.9, max_tokens=1_000_000, max_seconds=600)
    ledger = BudgetLedger(max=budget)
    await ledger.commit(member="earlier-sibling", turns=0, tokens=0, cost_usd=0.95)
    ctx = ParentRuntime(budget=budget, budget_ledger=ledger)

    child = Agent(
        "worker",
        system_prompt="do work",
        config=AgentConfig(name="worker", llm=_plan_llm(), description="A PLAN_FIRST worker"),
    )
    pipeline = Spawn([child], llm=_plan_llm(), hooks=None, framework_config=None, ctx=ctx)

    result = await pipeline.spawn("worker", "do something")

    # The reserve gate rejects the spawn pre-flight — sibling spend is already
    # at cap, so the child never burns a single token of its own.
    assert result.get("code") == "spawn_budget_exhausted", result


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
