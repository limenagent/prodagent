"""Cross-peer budget aggregation (peers=) — RunLoop._peer_budget.

Before this, ``peers=`` had no budget aggregation at all: each hop got its own
fresh HardBudget check, so an N-hop chain could legally spend N times the
configured budget — only ``max_peer_chain`` capped the number of hops, not
cumulative spend. These tests drive a real peer chain end to end and assert
the chain stops once *cumulative* turns across hops hit the root's ceiling,
even though no single hop's own run exceeds it.
"""

from __future__ import annotations

import pytest

from prodagent import Agent, AgentConfig
from prodagent.kernel.budget import HardBudget
from prodagent.kernel.run import is_child_run_id
from prodagent.llm.fake import script


def _reactive_agent(name: str, *, context: str = "", peers=None) -> Agent:
    return Agent(
        name,
        system_prompt=context,
        config=AgentConfig(name=name, peers=list(peers or [])),
    )


@pytest.mark.asyncio
async def test_peer_chain_stops_on_cumulative_turns_even_if_each_hop_is_under_cap():
    peer_c = _reactive_agent("C", context="you are C")
    peer_c.config.llm = script({"content": "C final answer"})

    peer_b = _reactive_agent("B", context="you are B", peers=[peer_c])
    peer_b.config.llm = script({"tool": "handoff_to_C", "params": {"task": "C handle this"}})

    agent_a = _reactive_agent("A", context="you are A", peers=[peer_b])
    agent_a.config.llm = script({"tool": "handoff_to_B", "params": {"task": "B handle this"}})
    agent_a.config.budget = HardBudget(max_turns=2, max_cost_usd=100, max_tokens=1_000_000)

    run = await agent_a.chat("start the chain", session_id="peer-budget-chain")

    # A took 1 turn, B took 1 turn — cumulative hits max_turns=2 before C ever runs.
    assert run.state.value == "completed"
    assert is_child_run_id(run.run_id)
    assert run.run_id.endswith("::B")
    assert run.final_output == "Handed off to C"


@pytest.mark.asyncio
async def test_peer_chain_completes_when_cumulative_turns_stay_under_cap():
    peer_c = _reactive_agent("C", context="you are C")
    peer_c.config.llm = script({"content": "C final answer"})

    peer_b = _reactive_agent("B", context="you are B", peers=[peer_c])
    peer_b.config.llm = script({"tool": "handoff_to_C", "params": {"task": "C handle this"}})

    agent_a = _reactive_agent("A", context="you are A", peers=[peer_b])
    agent_a.config.llm = script({"tool": "handoff_to_B", "params": {"task": "B handle this"}})
    agent_a.config.budget = HardBudget(max_turns=10, max_cost_usd=100, max_tokens=1_000_000)

    run = await agent_a.chat("start the chain", session_id="peer-budget-chain-ok")

    assert run.state.value == "completed"
    assert run.final_output == "C final answer"
    assert run.run_id.endswith("::C")
