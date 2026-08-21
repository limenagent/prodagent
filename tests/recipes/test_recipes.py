"""Recipe presets — offline smoke tests for the one-line assemblies."""

from __future__ import annotations

import pytest

from prodagent import Agent, AgentConfig, ExecutionMode, HardBudget, script, tool
from prodagent.recipes import audit_agent, delegation_agent, research_agent


@tool(name="lookup", readonly=True)
async def lookup(query: str) -> str:
    return f"result for {query}"


def test_audit_agent_pins_plan_first_and_defaults() -> None:
    agent = audit_agent("auditor", "Audit the ledger.", [lookup])
    assert agent.config.mode is ExecutionMode.PLAN_FIRST
    assert agent.config.budget is not None
    assert agent.config.budget.max_turns == 30
    # explicit budget wins over the preset default
    custom = audit_agent("auditor", "Audit.", [lookup], budget=HardBudget(max_turns=3))
    assert custom.config.budget.max_turns == 3


def test_research_agent_pins_reactive() -> None:
    agent = research_agent("scout", "Research the topic.", [lookup])
    assert agent.config.mode is ExecutionMode.REACTIVE
    assert agent.config.budget is not None


def test_delegation_agent_requires_topology() -> None:
    with pytest.raises(ValueError, match="agents=|peers="):
        delegation_agent("hub", "Coordinate.")


def test_delegation_agent_wires_children() -> None:
    child = Agent("worker", config=AgentConfig(name="worker", llm=script({"content": "ok"})))
    hub = delegation_agent("hub", "Coordinate.", agents=[child])
    assert [c.config.name for c in hub.config.agents] == ["worker"]
    assert hub.config.budget is not None


async def test_audit_agent_chats_offline() -> None:
    agent = audit_agent("auditor", "Audit.", [lookup])
    agent.config.llm = script({"content": "done"})
    run = await agent.chat("audit the Q3 ledger")
    assert run.state is not None  # end-to-end offline round-trip
