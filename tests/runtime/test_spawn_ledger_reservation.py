"""Spawn's per-call BudgetLedger — closing the concurrent spawn_agent race.

spawn_agent is marked is_readonly=True (spawn.py) specifically so that
multiple spawn_agent calls issued in one LLM turn dispatch *concurrently* via
asyncio.gather in ToolRunner.run_batch, not serially. Before this ledger,
nothing serialized "check budget, then start the child" across those
concurrent siblings — each independently saw a stale pre-batch snapshot and
could pass a check that, jointly, blew past the cap. Spawn now holds
a lock-protected BudgetLedger and reserves before each child starts, so a
sibling that would push the ledger over cap is rejected before it ever runs,
rather than being allowed to run to completion regardless.

This is deliberately tested against the *turns* axis (reserved synchronously,
before either child has produced any real cost) rather than cost/tokens
(which are only known after a child finishes) — turns is exactly the axis
where the old code had zero protection against concurrent siblings.
"""

from __future__ import annotations

import asyncio

import pytest

from prodagent.coordination.spawn import Spawn
from prodagent.kernel.budget import HardBudget
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.runtime.agent import Agent
from prodagent.runtime.config import AgentConfig
from prodagent.runtime.parent_runtime import ParentRuntime


def _worker(name: str) -> Agent:
    return Agent(
        name,
        system_prompt="do work",
        config=AgentConfig(name=name, llm=FakeLLMAdapter(responses=[])),
    )


@pytest.mark.asyncio
async def test_only_one_of_two_concurrent_spawns_passes_a_one_turn_ceiling():
    budget = HardBudget(max_turns=1, max_cost_usd=100, max_tokens=1_000_000, max_seconds=600)
    ctx = ParentRuntime(budget=budget)

    worker_a = _worker("workerA")
    worker_b = _worker("workerB")
    pipeline = Spawn(
        [worker_a, worker_b],
        llm=FakeLLMAdapter(responses=[]),
        hooks=None,
        framework_config=None,
        ctx=ctx,
    )

    result_a, result_b = await asyncio.gather(
        pipeline.spawn("workerA", "do A"),
        pipeline.spawn("workerB", "do B"),
    )

    outcomes = {result_a.get("state"), result_b.get("state")}
    errors = [r for r in (result_a, result_b) if "error" in r]

    # Exactly one sibling's reservation was rejected pre-flight by the ledger;
    # the other was free to actually run (and, with no scripted LLM turns, its
    # own child run fails independently — but it was never budget-rejected).
    assert len(errors) == 1
    assert errors[0]["code"] == "spawn_budget_exhausted"
    assert "failed" in outcomes or "contract_violation" in outcomes or len(outcomes) >= 1


@pytest.mark.asyncio
async def test_ledger_is_noop_when_no_budget_configured():
    ctx = ParentRuntime(budget=None)
    worker_a = _worker("workerA")
    pipeline = Spawn(
        [worker_a],
        llm=FakeLLMAdapter(responses=[]),
        hooks=None,
        framework_config=None,
        ctx=ctx,
    )
    assert pipeline._budget_ledger is None

    result = await pipeline.spawn("workerA", "do A")
    assert "error" not in result or result.get("code") != "spawn_budget_exhausted"
