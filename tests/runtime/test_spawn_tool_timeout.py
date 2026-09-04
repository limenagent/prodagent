"""The ``spawn_agent`` tool must outlive its child, not pre-empt it.

Regression for the incident where every ``spawn_agent`` call landed as
``tool_timeout`` and the breaker opened, leaving root cause "Unknown": the
tool inherited the 10s default deadline while the child's own clamp
(``activate_child``) is ``min(DEFAULT_TIMEOUT_S, budget_s)`` — so the
dispatcher cancelled the child before its first few turns finished.
"""

from __future__ import annotations

from types import SimpleNamespace

from prodagent import Agent
from prodagent.runtime.config import DEFAULT_TIMEOUT_S, AgentConfig
from prodagent.runtime.tools import assemble_peer_tools, assemble_spawn_tools


def _parent_with_child() -> Agent:
    child = Agent("remediator", system_prompt="investigate and fix")
    return Agent(config=AgentConfig(name="root", agents=[child], peers=[child]))


def test_spawn_agent_tool_timeout_exceeds_child_clamp() -> None:
    active: list = []
    schemas: list = []
    assemble_spawn_tools(SimpleNamespace(agent=_parent_with_child()), active, schemas)

    assert len(active) == 1
    tool = active[0]
    assert tool.name == "spawn_agent"
    # Must be strictly larger than the child's max clamp (DEFAULT_TIMEOUT_S),
    # or the dispatcher's wait_for beats activate_child's graceful return.
    assert tool.meta.timeout_seconds == DEFAULT_TIMEOUT_S


def test_handoff_tool_keeps_fast_default_timeout() -> None:
    active: list = []
    schemas: list = []
    assemble_peer_tools(SimpleNamespace(agent=_parent_with_child()), active, schemas)

    assert len(active) == 1
    assert active[0].name == "handoff_to_remediator"
    # Handoff just returns a ToolResult immediately — it must keep the fast
    # default, not inherit the spawn grace.
    assert active[0].meta.timeout_seconds == 10.0
